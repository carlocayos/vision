import datetime
import os
import time

import presets
import torch
import torch.utils.data
import torchvision
import torchvision.datasets.video_utils
import utils
from torch import nn
from torch.utils.data.dataloader import default_collate
from torchvision.datasets.samplers import DistributedSampler, UniformClipSampler, RandomClipSampler

try:
    from apex import amp
except ImportError:
    amp = None


def train_one_epoch(model, criterion, optimizer, lr_scheduler, data_loader, device, epoch, print_freq, apex=False):
    model.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter("lr", utils.SmoothedValue(window_size=1, fmt="{value}"))
    metric_logger.add_meter("clips/s", utils.SmoothedValue(window_size=10, fmt="{value:.3f}"))

    header = "Epoch: [{}]".format(epoch)
    for video, target in metric_logger.log_every(data_loader, print_freq, header):
        start_time = time.time()
        video, target = video.to(device), target.to(device)
        output = model(video)
        loss = criterion(output, target)

        optimizer.zero_grad()
        if apex:
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()
        optimizer.step()

        acc1, acc5 = utils.accuracy(output, target, topk=(1, 5))
        batch_size = video.shape[0]
        metric_logger.update(loss=loss.item(), lr=optimizer.param_groups[0]["lr"])
        metric_logger.meters["acc1"].update(acc1.item(), n=batch_size)
        metric_logger.meters["acc5"].update(acc5.item(), n=batch_size)
        metric_logger.meters["clips/s"].update(batch_size / (time.time() - start_time))
        lr_scheduler.step()


def evaluate(model, criterion, data_loader, device):
    model.eval()
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = "Test:"
    with torch.inference_mode():
        for video, target in metric_logger.log_every(data_loader, 100, header):
            video = video.to(device, non_blocking=True)
            target = target.to(device, non_blocking=True)
            output = model(video)
            loss = criterion(output, target)

            acc1, acc5 = utils.accuracy(output, target, topk=(1, 5))
            # FIXME need to take into account that the datasets
            # could have been padded in distributed setup
            batch_size = video.shape[0]
            metric_logger.update(loss=loss.item())
            metric_logger.meters["acc1"].update(acc1.item(), n=batch_size)
            metric_logger.meters["acc5"].update(acc5.item(), n=batch_size)
    # gather the stats from all processes
    metric_logger.synchronize_between_processes()

    print(
        " * Clip Acc@1 {top1.global_avg:.3f} Clip Acc@5 {top5.global_avg:.3f}".format(
            top1=metric_logger.acc1, top5=metric_logger.acc5
        )
    )
    return metric_logger.acc1.global_avg


def _get_cache_path(filepath):
    import hashlib

    h = hashlib.sha1(filepath.encode()).hexdigest()
    cache_path = os.path.join("~", ".torch", "vision", "datasets", "kinetics", h[:10] + ".pt")
    cache_path = os.path.expanduser(cache_path)
    return cache_path


def collate_fn(batch):
    # remove audio from the batch
    batch = [(d[0], d[2]) for d in batch]
    return default_collate(batch)


def main(args):
    if args.apex and amp is None:
        raise RuntimeError(
            "Failed to import apex. Please install apex from https://www.github.com/nvidia/apex "
            "to enable mixed-precision training."
        )

    if args.output_dir:
        utils.mkdir(args.output_dir)

    utils.init_distributed_mode(args)
    print(args)
    print("torch version: ", torch.__version__)
    print("torchvision version: ", torchvision.__version__)

    device = torch.device(args.device)

    torch.backends.cudnn.benchmark = True

    # Data loading code
    print("Loading data")
    traindir = os.path.join(args.data_path, args.train_dir)
    valdir = os.path.join(args.data_path, args.val_dir)

    print("Loading training data")
    st = time.time()
    cache_path = _get_cache_path(traindir)
    transform_train = presets.VideoClassificationPresetTrain((128, 171), (112, 112))

    if args.cache_dataset and os.path.exists(cache_path):
        print("Loading dataset_train from {}".format(cache_path))
        dataset, _ = torch.load(cache_path)
        dataset.transform = transform_train
    else:
        if args.distributed:
            print("It is recommended to pre-compute the dataset cache " "on a single-gpu first, as it will be faster")
        dataset = torchvision.datasets.Kinetics400(
            traindir,
            frames_per_clip=args.clip_len,
            step_between_clips=1,
            transform=transform_train,
            frame_rate=15,
            extensions=(
                "avi",
                "mp4",
            ),
        )
        if args.cache_dataset:
            print("Saving dataset_train to {}".format(cache_path))
            utils.mkdir(os.path.dirname(cache_path))
            utils.save_on_master((dataset, traindir), cache_path)

    print("Took", time.time() - st)

    print("Loading validation data")
    cache_path = _get_cache_path(valdir)

    transform_test = presets.VideoClassificationPresetEval((128, 171), (112, 112))

    if args.cache_dataset and os.path.exists(cache_path):
        print("Loading dataset_test from {}".format(cache_path))
        dataset_test, _ = torch.load(cache_path)
        dataset_test.transform = transform_test
    else:
        if args.distributed:
            print("It is recommended to pre-compute the dataset cache " "on a single-gpu first, as it will be faster")
        dataset_test = torchvision.datasets.Kinetics400(
            valdir,
            frames_per_clip=args.clip_len,
            step_between_clips=1,
            transform=transform_test,
            frame_rate=15,
            extensions=(
                "avi",
                "mp4",
            ),
        )
        if args.cache_dataset:
            print("Saving dataset_test to {}".format(cache_path))
            utils.mkdir(os.path.dirname(cache_path))
            utils.save_on_master((dataset_test, valdir), cache_path)

    print("Creating data loaders")
    train_sampler = RandomClipSampler(dataset.video_clips, args.clips_per_video)
    test_sampler = UniformClipSampler(dataset_test.video_clips, args.clips_per_video)
    if args.distributed:
        train_sampler = DistributedSampler(train_sampler)
        test_sampler = DistributedSampler(test_sampler)

    data_loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=args.batch_size,
        sampler=train_sampler,
        num_workers=args.workers,
        pin_memory=True,
        collate_fn=collate_fn,
    )

    data_loader_test = torch.utils.data.DataLoader(
        dataset_test,
        batch_size=args.batch_size,
        sampler=test_sampler,
        num_workers=args.workers,
        pin_memory=True,
        collate_fn=collate_fn,
    )

    print("Creating model")
    model = torchvision.models.video.__dict__[args.model](pretrained=args.pretrained)
    model.to(device)
    if args.distributed and args.sync_bn:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)

    criterion = nn.CrossEntropyLoss()

    lr = args.lr * args.world_size
    optimizer = torch.optim.SGD(model.parameters(), lr=lr, momentum=args.momentum, weight_decay=args.weight_decay)

    if args.apex:
        model, optimizer = amp.initialize(model, optimizer, opt_level=args.apex_opt_level)

    # convert scheduler to be per iteration, not per epoch, for warmup that lasts
    # between different epochs
    iters_per_epoch = len(data_loader)
    lr_milestones = [iters_per_epoch * (m - args.lr_warmup_epochs) for m in args.lr_milestones]
    main_lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=lr_milestones, gamma=args.lr_gamma)

    if args.lr_warmup_epochs > 0:
        warmup_iters = iters_per_epoch * args.lr_warmup_epochs
        args.lr_warmup_method = args.lr_warmup_method.lower()
        if args.lr_warmup_method == "linear":
            warmup_lr_scheduler = torch.optim.lr_scheduler.LinearLR(
                optimizer, start_factor=args.lr_warmup_decay, total_iters=warmup_iters
            )
        elif args.lr_warmup_method == "constant":
            warmup_lr_scheduler = torch.optim.lr_scheduler.ConstantLR(
                optimizer, factor=args.lr_warmup_decay, total_iters=warmup_iters
            )
        else:
            raise RuntimeError(
                "Invalid warmup lr method '{}'. Only linear and constant "
                "are supported.".format(args.lr_warmup_method)
            )

        lr_scheduler = torch.optim.lr_scheduler.SequentialLR(
            optimizer, schedulers=[warmup_lr_scheduler, main_lr_scheduler], milestones=[warmup_iters]
        )
    else:
        lr_scheduler = main_lr_scheduler

    model_without_ddp = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        model_without_ddp = model.module

    if args.resume:
        checkpoint = torch.load(args.resume, map_location="cpu")
        model_without_ddp.load_state_dict(checkpoint["model"])
        optimizer.load_state_dict(checkpoint["optimizer"])
        lr_scheduler.load_state_dict(checkpoint["lr_scheduler"])
        args.start_epoch = checkpoint["epoch"] + 1

    if args.test_only:
        evaluate(model, criterion, data_loader_test, device=device)
        return

    print("Start training")
    start_time = time.time()
    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)
        train_one_epoch(
            model, criterion, optimizer, lr_scheduler, data_loader, device, epoch, args.print_freq, args.apex
        )
        evaluate(model, criterion, data_loader_test, device=device)
        if args.output_dir:
            checkpoint = {
                "model": model_without_ddp.state_dict(),
                "optimizer": optimizer.state_dict(),
                "lr_scheduler": lr_scheduler.state_dict(),
                "epoch": epoch,
                "args": args,
            }
            utils.save_on_master(checkpoint, os.path.join(args.output_dir, "model_{}.pth".format(epoch)))
            utils.save_on_master(checkpoint, os.path.join(args.output_dir, "checkpoint.pth"))

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print("Training time {}".format(total_time_str))


def parse_args():
    import argparse

    parser = argparse.ArgumentParser(description="PyTorch Video Classification Training")

    parser.add_argument("--data-path", default="/datasets01_101/kinetics/070618/", type=str, help="dataset path")
    parser.add_argument("--train-dir", default="train_avi-480p", type=str, help="name of train dir")
    parser.add_argument("--val-dir", default="val_avi-480p", type=str, help="name of val dir")
    parser.add_argument("--model", default="r2plus1d_18", type=str, help="model name")
    parser.add_argument("--device", default="cuda", type=str, help="device (Use cuda or cpu Default: cuda)")
    parser.add_argument("--clip-len", default=16, type=int, metavar="N", help="number of frames per clip")
    parser.add_argument(
        "--clips-per-video", default=5, type=int, metavar="N", help="maximum number of clips per video to consider"
    )
    parser.add_argument(
        "-b", "--batch-size", default=24, type=int, help="images per gpu, the total batch size is $NGPU x batch_size"
    )
    parser.add_argument("--epochs", default=45, type=int, metavar="N", help="number of total epochs to run")
    parser.add_argument(
        "-j", "--workers", default=10, type=int, metavar="N", help="number of data loading workers (default: 10)"
    )
    parser.add_argument("--lr", default=0.01, type=float, help="initial learning rate")
    parser.add_argument("--momentum", default=0.9, type=float, metavar="M", help="momentum")
    parser.add_argument(
        "--wd",
        "--weight-decay",
        default=1e-4,
        type=float,
        metavar="W",
        help="weight decay (default: 1e-4)",
        dest="weight_decay",
    )
    parser.add_argument("--lr-milestones", nargs="+", default=[20, 30, 40], type=int, help="decrease lr on milestones")
    parser.add_argument("--lr-gamma", default=0.1, type=float, help="decrease lr by a factor of lr-gamma")
    parser.add_argument("--lr-warmup-epochs", default=10, type=int, help="the number of epochs to warmup (default: 10)")
    parser.add_argument("--lr-warmup-method", default="linear", type=str, help="the warmup method (default: linear)")
    parser.add_argument("--lr-warmup-decay", default=0.001, type=float, help="the decay for lr")
    parser.add_argument("--print-freq", default=10, type=int, help="print frequency")
    parser.add_argument("--output-dir", default=".", type=str, help="path to save outputs")
    parser.add_argument("--resume", default="", type=str, help="path of checkpoint")
    parser.add_argument("--start-epoch", default=0, type=int, metavar="N", help="start epoch")
    parser.add_argument(
        "--cache-dataset",
        dest="cache_dataset",
        help="Cache the datasets for quicker initialization. It also serializes the transforms",
        action="store_true",
    )
    parser.add_argument(
        "--sync-bn",
        dest="sync_bn",
        help="Use sync batch norm",
        action="store_true",
    )
    parser.add_argument(
        "--test-only",
        dest="test_only",
        help="Only test the model",
        action="store_true",
    )
    parser.add_argument(
        "--pretrained",
        dest="pretrained",
        help="Use pre-trained models from the modelzoo",
        action="store_true",
    )

    # Mixed precision training parameters
    parser.add_argument("--apex", action="store_true", help="Use apex for mixed precision training")
    parser.add_argument(
        "--apex-opt-level",
        default="O1",
        type=str,
        help="For apex mixed precision training"
        "O0 for FP32 training, O1 for mixed precision training."
        "For further detail, see https://github.com/NVIDIA/apex/tree/master/examples/imagenet",
    )

    # distributed training parameters
    parser.add_argument("--world-size", default=1, type=int, help="number of distributed processes")
    parser.add_argument("--dist-url", default="env://", type=str, help="url used to set up distributed training")

    args = parser.parse_args()

    return args


if __name__ == "__main__":
    args = parse_args()
    main(args)
