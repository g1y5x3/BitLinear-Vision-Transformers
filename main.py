import datetime
import json
import time
from torch.utils.data import DataLoader, DistributedSampler
import datasets

import torch, random, argparse
import numpy as np
from pathlib import Path

import util.misc as utils
from models.backbone import ResNetBackbone
from models.transformer import Transformer, TransformerBitLinear
from models.detr import DETR, SetCriterion
from models.matcher import HungarianMatcher
from datasets import build_dataset, get_coco_api_from_dataset
from engine import train_one_epoch

def get_args_parser():
	parser = argparse.ArgumentParser('Set transformer detector', add_help=False)
	parser.add_argument('--lr', default=1e-4, type=float)
	parser.add_argument('--lr_backbone', default=1e-5, type=float)
	parser.add_argument('--batch_size', default=16, type=int)
	parser.add_argument('--weight_decay', default=1e-4, type=float)
	parser.add_argument('--epochs', default=300, type=int)
	parser.add_argument('--lr_drop', default=200, type=int)
	parser.add_argument('--clip_max_norm', default=0.1, type=float,
											help='gradient clipping max norm')
	# Backbone
	parser.add_argument('--backbone', default='resnet50', type=str,
											help="Name of the convolutional backbone to use")
	# Transformer
	parser.add_argument('--enc_layers', default=6, type=int,
											help="Number of encoding layers in the transformer")
	parser.add_argument('--dec_layers', default=6, type=int,
											help="Number of decoding layers in the transformer")
	parser.add_argument('--dim_feedforward', default=2048, type=int,
											help="Intermediate size of the feedforward layers in the transformer blocks")
	parser.add_argument('--hidden_dim', default=256, type=int,
											help="Size of the embeddings (dimension of the transformer)")
	parser.add_argument('--dropout', default=0.1, type=float,
											help="Dropout applied in the transformer")
	parser.add_argument('--nheads', default=8, type=int,
											help="Number of attention heads inside the transformer's attentions")
	parser.add_argument('--num_queries', default=100, type=int,
											help="Number of query slots")
	# Matcher
	parser.add_argument('--cost_class', default=1, type=float,
											help="Class coefficient in the matching cost")
	parser.add_argument('--cost_bbox', default=5, type=float,
											help="L1 box coefficient in the matching cost")
	parser.add_argument('--cost_giou', default=2, type=float,
											help="giou box coefficient in the matching cost")
	# Loss coefficients
	parser.add_argument('--dice_loss_coef', default=1, type=float)
	parser.add_argument('--bbox_loss_coef', default=5, type=float)
	parser.add_argument('--giou_loss_coef', default=2, type=float)
	parser.add_argument('--eos_coef', default=0.1, type=float,
											help="Relative classification weight of the no-object class")
	# Dataset parameters
	parser.add_argument('--dataset_file', default='coco')
	parser.add_argument('--coco_path', type=str)
	# Others
	parser.add_argument('--output_dir', default='',
											help='path where to save, empty for no saving')
	parser.add_argument('--device', default='cuda',
											help='device to use for training / testing')
	parser.add_argument('--seed', default=42, type=int)
	parser.add_argument('--resume', default='', help='resume from checkpoint')
	parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
											help='start epoch')
	parser.add_argument('--eval', action='store_true')
	parser.add_argument('--num_workers', default=2, type=int)
	# Distributed training parameters
	parser.add_argument('--world_size', default=1, type=int,
											help='number of distributed processes')
  # Segmentation
	parser.add_argument('--masks', action='store_true',
											help="Train segmentation head if the flag is provided")
	return parser


def main(args):
	print("git:\n  {}\n".format(utils.get_sha()))
	device = torch.device(args.device)

  # fix the seed for reproducibility (and your sanity)
	seed = args.seed + utils.get_rank()
	torch.manual_seed(seed)
	np.random.seed(seed)
	random.seed(seed)
    
	# model and criterion
	backbone = ResNetBackbone()		# currently not using the args
	transformer = TransformerBitLinear(args.hidden_dim, args.nheads, args.enc_layers, args.dec_layers, args.dim_feedforward, args.dropout)
	model = DETR(backbone=backbone, transformer=transformer, num_classes=91, num_queries=args.num_queries)
	matcher = HungarianMatcher(args.cost_class, args.cost_bbox, args.cost_giou)
	criterion = SetCriterion(91, matcher, args.eos_coef, (args.dice_loss_coef, args.bbox_loss_coef, args.giou_loss_coef))

	model.to(device)
	criterion.to(device)

	n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
	print('number of params:', n_parameters)

	# optimizer
	param_dicts = [
	  {"params": [p for n, p in model.named_parameters() if "backbone" not in n and p.requires_grad]},
	  {"params": [p for n, p in model.named_parameters() if "backbone" in n and p.requires_grad], "lr": args.lr_backbone},
	]

	optimizer = torch.optim.AdamW(param_dicts, lr=args.lr, weight_decay=args.weight_decay)
	scaler = torch.cuda.amp.GradScaler()
	lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, args.lr_drop)

	# create dataset
	dataset_train = build_dataset(image_set='train', args=args)
	dataset_val   = build_dataset(image_set='val', args=args)
	sampler_train = torch.utils.data.RandomSampler(dataset_train)
	sampler_val   = torch.utils.data.SequentialSampler(dataset_val)

	batch_sampler_train = torch.utils.data.BatchSampler(sampler_train, args.batch_size, drop_last=True)

	data_loader_train = DataLoader(dataset_train, batch_sampler=batch_sampler_train, 
																 collate_fn=utils.collate_fn, num_workers=args.num_workers)
	data_loader_val   = DataLoader(dataset_val, args.batch_size, sampler=sampler_val, drop_last=False,
															   collate_fn=utils.collate_fn, num_workers=args.num_workers)

	output_dir = Path(args.output_dir)
	print("Start training")
	start_time = time.time()

	for epoch in range(args.start_epoch, args.epochs):
	  # if args.distributed: sampler_train.set_epoch(epoch)
	  train_stats = train_one_epoch(model, criterion, data_loader_train, optimizer, scaler, device, epoch, args.clip_max_norm)
	  lr_scheduler.step()

	total_time = time.time() - start_time
	total_time_str = str(datetime.timedelta(seconds=int(total_time)))
	print('Training time {}'.format(total_time_str))		

	  #if args.output_dir:
	  #  checkpoint_paths = [output_dir / 'checkpoint.pth']
	  #  # extra checkpoint before LR drop and every 100 epochs
	  #  if (epoch + 1) % args.lr_drop == 0 or (epoch + 1) % 100 == 0:
	  #    checkpoint_paths.append(output_dir / f'checkpoint{epoch:04}.pth')
	  #  for checkpoint_path in checkpoint_paths:
	  #    utils.save_on_master({
	  #      'model': model_without_ddp.state_dict(),
	  #      'optimizer': optimizer.state_dict(),
	  #      'lr_scheduler': lr_scheduler.state_dict(),
	  #      'epoch': epoch,
	  #      'args': args,
	  #    }, checkpoint_path)

	    #test_stats, coco_evaluator = evaluate(model, criterion, postprocessors, data_loader_val, base_ds, device, args.output_dir)

	    #print(test_stats)

	    #log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
	    #             **{f'test_{k}': v for k, v in test_stats.items()},
	    #             'epoch': epoch,
	    #             'n_parameters': n_parameters}

	    #if utils.is_main_process():
	    #    run.log({
	    #        "test/class_error": test_stats["class_error"],
	    #        "test/loss": test_stats["loss"],
	    #        "test/loss_bbox": test_stats["loss_bbox"],
	    #        "test/loss_ce": test_stats["loss_ce"],
	    #        "test/loss_giou": test_stats["loss_giou"]
	    #    })

	    #    run.log({
	    #        "metrics/AP@0.5:0.95": test_stats["coco_eval_bbox"][0],
	    #        "metrics/AP@0.5": test_stats["coco_eval_bbox"][1],
	    #        "metrics/AP@0.75": test_stats["coco_eval_bbox"][2],
	    #    })

	#model, criterion, postprocessors = build_model(args)
	#model.to(device)

	#model_without_ddp = model
	#if args.distributed:
	#    model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
	#    model_without_ddp = model.module

	#if args.distributed:
	#    sampler_train = DistributedSampler(dataset_train)
	#    sampler_val = DistributedSampler(dataset_val, shuffle=False)
	#else:
	#if args.dataset_file == "coco_panoptic":
	#    # We also evaluate AP during panoptic training, on original coco DS
	#    coco_val = datasets.coco.build("val", args)
	#    base_ds = get_coco_api_from_dataset(coco_val)
	#else:
	#    base_ds = get_coco_api_from_dataset(dataset_val)

	#if args.frozen_weights is not None:
	#    checkpoint = torch.load(args.frozen_weights, map_location='cpu')
	#    model_without_ddp.detr.load_state_dict(checkpoint['model'])

	#if args.resume:
	#    if args.resume.startswith('https'):
	#        checkpoint = torch.hub.load_state_dict_from_url(
	#            args.resume, map_location='cpu', check_hash=True)
	#    else:
	#        checkpoint = torch.load(args.resume, map_location='cpu')
	#    model_without_ddp.load_state_dict(checkpoint['model'])
	#    if not args.eval and 'optimizer' in checkpoint and 'lr_scheduler' in checkpoint and 'epoch' in checkpoint:
	#        optimizer.load_state_dict(checkpoint['optimizer'])
	#        lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
	#        args.start_epoch = checkpoint['epoch'] + 1

	#if args.eval:
	#    test_stats, coco_evaluator = evaluate(model, criterion, postprocessors,
	#                                          data_loader_val, base_ds, device, args.output_dir)
	#    if args.output_dir:
	#        utils.save_on_master(coco_evaluator.coco_eval["bbox"].eval, output_dir / "eval.pth")
	#    return



	#    print(coco_evaluator)

	#    if args.output_dir and utils.is_main_process():
	#        with (output_dir / "log.txt").open("a") as f:
	#            f.write(json.dumps(log_stats) + "\n")

	#        # for evaluation logs
	#        if coco_evaluator is not None:
	#            (output_dir / 'eval').mkdir(exist_ok=True)
	#            if "bbox" in coco_evaluator.coco_eval:
	#                filenames = ['latest.pth']
	#                if epoch % 50 == 0:
	#                    filenames.append(f'{epoch:03}.pth')
	#                for name in filenames:
	#                    torch.save(coco_evaluator.coco_eval["bbox"].eval,
	#                               output_dir / "eval" / name)

	#total_time = time.time() - start_time
	#total_time_str = str(datetime.timedelta(seconds=int(total_time)))
	#print('Training time {}'.format(total_time_str))

	#if utils.is_main_process():
	#    run.finish()

if __name__ == '__main__':
	parser = argparse.ArgumentParser('DETR training and evaluation script', parents=[get_args_parser()])
	args = parser.parse_args()
	if args.output_dir: Path(args.output_dir).mkdir(parents=True, exist_ok=True)
	main(args)