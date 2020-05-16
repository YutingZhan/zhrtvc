#!usr/bin/env python
# -*- coding: utf-8 -*-
# author: kuangdd
# date: 2020/2/21
"""
"""
from melgan.mel2wav.dataset import AudioDataset
from melgan.mel2wav.modules import Generator, Discriminator, Audio2Mel
from melgan.mel2wav.utils import save_sample

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

import yaml
import json
import numpy as np
import time
import argparse
from pathlib import Path

from tqdm import tqdm
from aukit.audio_griffinlim import mel_spectrogram, default_hparams

my_hp = {
    "n_fft": 1024, "hop_size": 256, "win_size": 1024,
    "sample_rate": 22050, "max_abs_value": 4.0,
    "fmin": 0, "fmax": 8000,
    "preemphasize": True,
    'symmetric_mels': True,
}
default_hparams.update(my_hp)

_pad_len = (default_hparams.n_fft - default_hparams.hop_size) // 2


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--save_path", default=r"E:\lab\melgan\logs\alijuzi")
    parser.add_argument("--load_path", default=r"E:\lab\melgan\logs\publish")  # r"E:\lab\melgan\logs\publish"
    parser.add_argument("--start_step", default=0)

    parser.add_argument("--n_mel_channels", type=int, default=80)
    parser.add_argument("--ngf", type=int, default=32)
    parser.add_argument("--n_residual_layers", type=int, default=3)

    parser.add_argument("--ndf", type=int, default=16)
    parser.add_argument("--num_D", type=int, default=3)
    parser.add_argument("--n_layers_D", type=int, default=4)
    parser.add_argument("--downsamp_factor", type=int, default=4)
    parser.add_argument("--lambda_feat", type=float, default=10)
    parser.add_argument("--cond_disc", action="store_true")

    parser.add_argument("--data_path", type=str, default=r"E:\data\aliaudio\alijuzi")
    parser.add_argument("--batch_size", type=int, default=64)
    parser.add_argument("--seq_len", type=int, default=8192)

    parser.add_argument("--epochs", type=int, default=3000)
    parser.add_argument("--log_interval", type=int, default=100)
    parser.add_argument("--save_interval", type=int, default=1000)
    parser.add_argument("--n_test_samples", type=int, default=4)
    args = parser.parse_args()
    return args




def audio2mel(src):
    # print("wav", src.shape)
    # mel = Audio2Mel().cuda()(src)
    # print("mel", mel.cpu().shape)
    # return mel

    # wavs = F.pad(src, (p, p), "reflect")
    wavs = src.cpu().numpy()

    mels = []
    for wav in wavs:
        wav = np.pad(wav.flatten(), (_pad_len, _pad_len), mode="reflect")
        mel = mel_spectrogram(wav, default_hparams)
        mels.append(mel)
    mels = torch.from_numpy(np.array(mels).astype(np.float32))
    return mels


def train_melgan(args):
    # args = parse_args()

    root = Path(args.save_path)
    load_root = Path(args.load_path) if args.load_path else None
    root.mkdir(parents=True, exist_ok=True)

    ####################################
    # Dump arguments and create logger #
    ####################################
    # with open(root / "args.yml", "w") as f:
    #     yaml.dump(args, f)
    with open(root / "args.json", "w", encoding="utf8") as f:
        json.dump(args.__dict__, f, indent=4, ensure_ascii=False)
    eventdir = root / "events"
    eventdir.mkdir(exist_ok=True)
    writer = SummaryWriter(str(eventdir))

    #######################
    # Load PyTorch Models #
    #######################
    netG = Generator(args.n_mel_channels, args.ngf, args.n_residual_layers).cuda()
    netD = Discriminator(
        args.num_D, args.ndf, args.n_layers_D, args.downsamp_factor
    ).cuda()
    # fft = Audio2Mel(n_mel_channels=args.n_mel_channels).cuda()
    fft = audio2mel
    # print(netG)
    # print(netD)

    #####################
    # Create optimizers #
    #####################
    optG = torch.optim.Adam(netG.parameters(), lr=1e-4, betas=(0.5, 0.9))
    optD = torch.optim.Adam(netD.parameters(), lr=1e-4, betas=(0.5, 0.9))

    if load_root and load_root.exists():
        netG.load_state_dict(torch.load(load_root / "netG.pt"))
        # optG.load_state_dict(torch.load(load_root / "optG.pt"))
        # netD.load_state_dict(torch.load(load_root / "netD.pt"))
        # optD.load_state_dict(torch.load(load_root / "optD.pt"))

    #######################
    # Create data loaders #
    #######################
    train_set = AudioDataset(
        Path(args.data_path) / "train_files.txt", args.seq_len, sampling_rate=22050
    )
    test_set = AudioDataset(
        Path(args.data_path) / "test_files.txt",
        22050 * 4,
        sampling_rate=22050,
        augment=False,
    )

    train_loader = DataLoader(train_set, batch_size=args.batch_size, num_workers=4)
    test_loader = DataLoader(test_set, batch_size=1)

    ##########################
    # Dumping original audio #
    ##########################
    test_voc = []
    test_audio = []
    for i, x_t in enumerate(test_loader):
        x_t = x_t.cuda()
        s_t = fft(x_t).detach()

        test_voc.append(s_t.cuda())
        test_audio.append(x_t)

        audio = x_t.squeeze().cpu()
        oridir = root / "original"
        oridir.mkdir(exist_ok=True)
        save_sample(oridir / ("original_{}_{}.wav".format("test", i)), 22050, audio)
        writer.add_audio("original/{}/sample_{}.wav".format("test", i), audio, 0, sample_rate=22050)

        if i == args.n_test_samples - 1:
            break

    costs = []
    start = time.time()

    # enable cudnn autotuner to speed up training
    torch.backends.cudnn.benchmark = True

    best_mel_reconst = 1000000
    step_begin = args.start_step
    look_steps = {step_begin + 10, step_begin + 100, step_begin + 1000, step_begin + 10000}
    steps = step_begin
    for epoch in range(1, args.epochs + 1):
        print("\nEpoch {} beginning. Current step: {}".format(epoch, steps))
        for iterno, x_t in enumerate(tqdm(train_loader, desc="iter", ncols=100)):
            x_t = x_t.cuda()
            s_t = fft(x_t).detach()
            x_pred_t = netG(s_t.cuda())

            with torch.no_grad():
                s_pred_t = fft(x_pred_t.detach())
                s_error = F.l1_loss(s_t, s_pred_t).item()

            #######################
            # Train Discriminator #
            #######################
            D_fake_det = netD(x_pred_t.cuda().detach())
            D_real = netD(x_t.cuda())

            loss_D = 0
            for scale in D_fake_det:
                loss_D += F.relu(1 + scale[-1]).mean()

            for scale in D_real:
                loss_D += F.relu(1 - scale[-1]).mean()

            netD.zero_grad()
            loss_D.backward()
            optD.step()

            ###################
            # Train Generator #
            ###################
            D_fake = netD(x_pred_t.cuda())

            loss_G = 0
            for scale in D_fake:
                loss_G += -scale[-1].mean()

            loss_feat = 0
            feat_weights = 4.0 / (args.n_layers_D + 1)
            D_weights = 1.0 / args.num_D
            wt = D_weights * feat_weights
            for i in range(args.num_D):
                for j in range(len(D_fake[i]) - 1):
                    loss_feat += wt * F.l1_loss(D_fake[i][j], D_real[i][j].detach())

            netG.zero_grad()
            (loss_G + args.lambda_feat * loss_feat).backward()
            optG.step()

            ######################
            # Update tensorboard #
            ######################

            costs.append([loss_D.item(), loss_G.item(), loss_feat.item(), s_error])
            steps += 1
            writer.add_scalar("loss/discriminator", costs[-1][0], steps)
            writer.add_scalar("loss/generator", costs[-1][1], steps)
            writer.add_scalar("loss/feature_matching", costs[-1][2], steps)
            writer.add_scalar("loss/mel_reconstruction", costs[-1][3], steps)

            if steps % args.save_interval == 0 or steps in look_steps:
                st = time.time()
                with torch.no_grad():
                    for i, (voc, _) in enumerate(zip(test_voc, test_audio)):
                        pred_audio = netG(voc)
                        pred_audio = pred_audio.squeeze().cpu()
                        gendir = root / "generated"
                        gendir.mkdir(exist_ok=True)
                        save_sample(gendir / ("generated_step{}_{}.wav".format(steps, i)), 22050, pred_audio)
                        writer.add_audio(
                            "generated/step{}/sample_{}.wav".format(steps, i),
                            pred_audio,
                            epoch,
                            sample_rate=22050,
                        )

                ptdir = root / "models"
                ptdir.mkdir(exist_ok=True)
                torch.save(netG.state_dict(), ptdir / "step{}_netG.pt".format(steps))
                torch.save(optG.state_dict(), ptdir / "step{}_optG.pt".format(steps))

                torch.save(netD.state_dict(), ptdir / "step{}_netD.pt".format(steps))
                torch.save(optD.state_dict(), ptdir / "step{}_optD.pt".format(steps))

                if np.asarray(costs).mean(0)[-1] < best_mel_reconst:
                    best_mel_reconst = np.asarray(costs).mean(0)[-1]
                    torch.save(netD.state_dict(), ptdir / "best_step{}_netD.pt".format(steps))
                    torch.save(netG.state_dict(), ptdir / "best_step{}_netG.pt".format(steps))
                # print("\nTook %5.4fs to generate samples" % (time.time() - st))
                # print("-" * 100)

            if steps % args.log_interval == 0 or steps in look_steps:
                print(
                    "\nEpoch {} | Iters {} / {} | ms/batch {:5.2f} | loss {}".format(
                        epoch,
                        iterno,
                        len(train_loader),
                        1000 * (time.time() - start) / args.log_interval,
                        np.asarray(costs).mean(0),
                    )
                )
                costs = []
                start = time.time()


if __name__ == "__main__":
    main()