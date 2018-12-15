# Training file for MLP model
from datetime import datetime
import configargparse
from os.path import join
import os
from collections import defaultdict

import numpy as np
import torch

from src.utils.utils import str2bool, pickle_load, load_data, send_to_cuda
from src.train.dataset import Dataset
from src.train.validator import Validator
from src.models.mlpmodel import MLPModel
from src.utils.logger import get_logger
from src.train.trainer import Trainer
from src.utils.file import FileObjectStore

np.warnings.filterwarnings('ignore')


def parse_args():
    parser = configargparse.ArgumentParser(description='Training Wikinet 2',
                                           formatter_class=configargparse.ArgumentDefaultsHelpFormatter)
    # General
    general = parser.add_argument_group('General Settings.')
    general.add_argument('--my-config', required=True, is_config_file=True, help='config file path')
    general.add_argument('--exp_name', type=str, default="debug", help="Experiment name")
    general.add_argument("--debug", type=str2bool, default=True, help="whether to debug")

    # Data
    data = parser.add_argument_group('Data Settings.')
    data.add_argument('--data_path', required=True, type=str, help='location of data dir')
    data.add_argument('--yamada_model', type=str, help='name of yamada model')
    data.add_argument('--data_type', type=str, choices=['conll', 'wiki', 'proto'], help='whether to train with conll or wiki')
    data.add_argument('--train_size', type=int, help='number of training abstracts')
    data.add_argument('--mmaps', type=str2bool, help='use dicts or mmaps')
    data.add_argument('--data_types', type=str, help='name of datasets separated by comma')

    # Max Padding
    padding = parser.add_argument_group('Max Padding for batch.')
    padding.add_argument('--max_context_size', type=int, help='max number of context')
    padding.add_argument('--max_ent_size', type=int, help='max number of entities considered in abstract')
    padding.add_argument('--num_docs', type=int, help='max number of docs to use to create corpus vec')
    padding.add_argument('--ignore_init', type=str2bool, help='whether to ignore first five tokens of context')

    # Model Type
    model_selection = parser.add_argument_group('Type of model to train.')
    model_selection.add_argument('--pre_train', type=str, help='if specified, model will load state dict, must be ckpt')

    # Model params
    model_params = parser.add_argument_group("Parameters for chosen model.")
    model_params.add_argument('--dp', type=float, help='drop out')
    model_params.add_argument('--hidden_size', type=int, help='size of hidden layer in yamada model')

    # Candidate Generation
    candidate = parser.add_argument_group('Candidate generation.')
    candidate.add_argument('--cand_type', choices=['necounts', 'pershina'], help='whether to use pershina candidates')
    candidate.add_argument("--num_candidates", type=int, default=32, help="Total number of candidates")
    candidate.add_argument("--prop_gen_candidates", type=float, default=0.5, help="Proportion of candidates generated")
    candidate.add_argument("--coref", type=str2bool, default=False, help="Whether to use coref cands")

    # Training
    training = parser.add_argument_group("Training parameters.")
    training.add_argument("--num_epochs", type=int, default=5, help="Number of epochs")
    training.add_argument("--save_every", type=int, default=5, help="how often to checkpoint")
    training.add_argument("--patience", type=int, default=5, help="Patience for early stopping")
    training.add_argument("--batch_size", type=int, default=32, help="Batch size")
    training.add_argument("--num_workers", type=int, default=4, help="number of workers for data loader")
    training.add_argument('--lr', type=float, help='learning rate')
    training.add_argument('--wd', type=float, help='weight decay')
    training.add_argument('--embs_optim', type=str, choices=['adagrad', 'adam', 'rmsprop', 'sparseadam'],
                              help='optimizer for embeddings')
    training.add_argument('--other_optim', type=str, choices=['adagrad', 'adam', 'rmsprop'],
                              help='optimizer for paramaters that are not embeddings')
    training.add_argument('--sparse', type=str2bool, help='sparse gradients')

    # cuda
    parser.add_argument("--device", type=str, help="cuda device")
    parser.add_argument("--use_cuda", type=str2bool, help="use gpu or not")
    parser.add_argument("--profile", type=str2bool, help="if set will run profiler on dataloader and exit")

    args = parser.parse_args()
    logger = get_logger(args)

    if args.wd > 0:
        assert not args.sparse

    if args.use_cuda:
        devices = args.device.split(",")
        if len(devices) > 1:
            devices = tuple([int(device) for device in devices])
        else:
            devices = int(devices[0])
        args.__dict__['device'] = devices

    logger.info("Experiment Parameters:")
    print()
    for arg in sorted(vars(args)):
        logger.info('{:<15}\t{}'.format(arg, getattr(args, arg)))

    model_date_dir = join(args.data_path, 'models', '{}'.format(datetime.now().strftime("%Y_%m_%d")))
    if not os.path.exists(model_date_dir):
        os.makedirs(model_date_dir)
    model_dir = join(model_date_dir, args.exp_name)
    args.__dict__['model_dir'] = model_dir
    if not os.path.exists(model_dir):
        os.makedirs(model_dir)

    return args, logger, model_dir


def setup(args, logger):

    print()
    logger.info("Loading pre trained model at models/conll_v0.1.pt.....")
    state_dict = torch.load(join(args.data_path, 'models/conll_v0.1.pt'), map_location='cpu')['state_dict']
    ent_embs = state_dict['ent_embs.weight']
    word_embs = state_dict['word_embs.weight']
    logger.info("Model loaded.")

    dicts = {}
    for dict_name in ['str_prior', 'str_cond', 'str_necounts', 'redirects', 'disamb' 'ent_dict', 'word_dict']:
        dicts[dict_name] = FileObjectStore(join(args.data_path, "mmaps", dict_name))

    logger.info("Using {} for training.....".format(args.data_type))
    data = defaultdict(dict)

    for data_type in args.data_types.split(','):
        if data_type == 'wiki':
            res = load_data(args.data_type, args.train_size, args.data_path, coref=args.coref)
            id2context, examples = res['dev']
            new_examples = [examples[idx] for idx in np.random.randint(0, len(examples), 10000)]
            res['dev'] = id2context, new_examples
            for split, data_split in res.items():
                data['wiki'][split] = data_split
        elif data_type == 'conll':
            res = load_data('conll', args, args.data_path, coref=args.coref)
            for split, data_split in res.items():
                data['conll'][split] = data_split
        else:
            if args.coref:
                data[data_type]['dev'] = pickle_load(join(args.data_path, f'training_files/coref/{data_type}.pickle'))
            else:
                data[data_type]['dev'] = pickle_load(join(args.data_path, f'training_files/{data_type}.pickle'))

    if args.data_type == 'conll':
        train_data = data['conll']['train']
    else:
        train_data = data['wiki']['train']
    logger.info("Data loaded.")

    logger.info("Creating data loaders and validators.....")
    train_dataset = Dataset(data=train_data,
                            split='train',
                            data_type=args.data_type,
                            args=args,
                            cand_type=(args.cand_type if args.data_type == 'conll' else 'necounts'),
                            coref=(args.coref if args.data_type != 'wiki' else False),
                            **dicts)
    logger.info("Training dataset created. There will be {len(se")

    datasets = {}
    for data_type in args.data_types.split(','):
        datasets[data_type] = Dataset(data=data[data_type]['dev'],
                                      split='dev',
                                      data_type=args.data_type,
                                      args=args,
                                      cand_type=(args.cand_type if args.data_type == 'conll' else 'necounts'),
                                      coref=(args.coref if args.data_type != 'wiki' else False),
                                      **dicts)
        logger.info(f"{data_type} dev dataset created.")

    return train_dataset, datasets, word_embs, ent_embs, dicts


def get_model(args, word_embs, ent_embs,logger):

    model = MLPModel(word_embs=word_embs,
                     ent_embs=ent_embs,
                     args=args)

    if args.use_cuda:
        model = send_to_cuda(args.device, model)
    logger.info('Model created.')

    return model


def train(model=None,
          logger=None,
          datasets=None,
          train_dataset=None,
          args=None,
          dicts=None,
          run=None):

    train_loader = train_dataset.get_loader(batch_size=args.batch_size,
                                            shuffle=False,
                                            num_workers=args.num_workers,
                                            drop_last=False)
    logger.info("Data loaders and validators created.There will be {} batches.".format(len(train_loader)))

    logger.info("Starting validation for untrained model.")
    validators = {}
    for data_type in args.data_types.split(','):
        loader = datasets[data_type].get_loader(batch_size=args.batch_size,
                                                shuffle=False,
                                                num_workers=args.num_workers,
                                                drop_last=False)
        logger.info(f'Len loader {data_type} : {len(loader)}')
        validators[data_type] = Validator(loader=loader,
                                          args=args,
                                          data_type=data_type,
                                          run=run,
                                          **dicts)

    trainer = Trainer(loader=train_loader,
                      args=args,
                      validator=validators,
                      model=model,
                      model_type='yamada',
                      profile=args.profile)
    logger.info("Starting Training:")
    print()
    trainer.train()
    logger.info("Finished Training")


if __name__ == '__main__':
    Args, Logger, Model_dir = parse_args()
    Train_dataset, Datasets, Word_embs, Ent_Embs, Dicts = setup(Args, Logger)

    Model = get_model(Args, Word_embs, Ent_Embs, Logger)
    if Args.pre_train:
        state_dict = torch.load(Args.pre_train, map_location=Args.device if Args.use_cuda else 'cpu')['state_dict']
        Model.load_state_dict(state_dict)
    train(model=Model,
          train_dataset=Train_dataset,
          datasets=Datasets,
          logger=Logger,
          args=Args,
          dicts=Dicts)
