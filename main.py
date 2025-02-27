import argparse
import datetime
import sys
import json
from collections import defaultdict
from pathlib import Path
from tempfile import mkdtemp
from torchvision import transforms
import PIL.Image as Image
from torch.utils.data import DataLoader

import numpy as np
import torch
from torch import optim
import torch.nn as nn
import models
import objectives
from utils import Logger, Timer, save_model, save_vars, unpack_data
from SVHNMNISTDataset import SVHNMNIST
import os

import nltk


parser = argparse.ArgumentParser(description='Multi-Modal VAEs')
parser.add_argument('--experiment', type=str, default='', metavar='E',
                    help='experiment name')
parser.add_argument('--model', type=str, default='mnist_svhn', metavar='M',
                    choices=[s[4:] for s in dir(models) if 'VAE_' in s],
                    help='model name (default: mnist_svhn)')
parser.add_argument('--obj', type=str, default='elbo', metavar='O',
                    choices=['elbo', 'iwae', 'dreg'],
                    help='objective to use (default: elbo)')
parser.add_argument('--K', type=int, default=20, metavar='K',
                    help='number of particles to use for iwae/dreg (default: 10)')
parser.add_argument('--looser', action='store_true', default=False,
                    help='use the looser version of IWAE/DREG')
parser.add_argument('--llik_scaling', type=float, default=0.,
                    help='likelihood scaling for cub images/svhn modality when running in'
                         'multimodal setting, set as 0 to use default value')
parser.add_argument('--batch-size', type=int, default=256, metavar='N',
                    help='batch size for data (default: 256)')
parser.add_argument('--epochs', type=int, default=50, metavar='E',
                    help='number of epochs to train (default: 10)')
parser.add_argument('--latent-dim', type=int, default=20, metavar='L',
                    help='latent dimensionality (default: 20)')
parser.add_argument('--num-hidden-layers', type=int, default=1, metavar='H',
                    help='number of hidden layers in enc and dec (default: 1)')
parser.add_argument('--pre-trained', type=str, default="",
                    help='path to pre-trained model (train from scratch if empty)')
parser.add_argument('--learn-prior', action='store_true', default=False,
                    help='learn model prior parameters')
parser.add_argument('--logp', action='store_true', default=False,
                    help='estimate tight marginal likelihood on completion')
parser.add_argument('--print-freq', type=int, default=0, metavar='f',
                    help='frequency with which to print stats (default: 0)')
parser.add_argument('--no-analytics', action='store_true', default=False,
                    help='disable plotting analytics')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='disable CUDA use')
parser.add_argument('--seed', type=int, default=1, metavar='S',
                    help='random seed (default: 1)')

nltk.download('punkt')
class _netE(nn.Module):
    def __init__(self):
        super().__init__()

        f = nn.LeakyReLU(0.2)

        self.ebm = nn.Sequential(
            nn.Linear(20, 200),
            f,

            nn.Linear(200, 200),
            f,

            nn.Linear(200, 1)
        )

    def forward(self, z):
        return self.ebm(z.squeeze()).view(-1, 1, 1, 1)



def get_transform_mnist():
    transform_mnist = transforms.Compose([transforms.ToTensor(),
                                          transforms.ToPILImage(),
                                          transforms.Resize(size=(28, 28), interpolation=Image.BICUBIC),
                                          transforms.ToTensor()])
    return transform_mnist

def get_transform_svhn():
    transform_svhn = transforms.Compose([transforms.ToTensor()])
    return transform_svhn

transform_mnist = get_transform_mnist()
transform_svhn = get_transform_svhn()
transforms = [transform_mnist, transform_svhn]

# args
args = parser.parse_args()

# random seed
# https://pytorch.org/docs/stable/notes/randomness.html
torch.backends.cudnn.benchmark = True
torch.manual_seed(args.seed)
np.random.seed(args.seed)

# load args from disk if pretrained model path is given
pretrained_path = ""
if args.pre_trained:
    pretrained_path = args.pre_trained
    args = torch.load(args.pre_trained + '/args.rar')

args.cuda = not args.no_cuda and torch.cuda.is_available()
device = torch.device("cuda" if args.cuda else "cpu")

# load model
modelC = getattr(models, 'VAE_{}'.format(args.model))
model = modelC(args).to(device)
#print(model)

if pretrained_path:
    print('Loading model {} from {}'.format(model.modelName, pretrained_path))
    model.load_state_dict(torch.load(pretrained_path + '/model.rar'))
    model._pz_params = model._pz_params

if not args.experiment:
    args.experiment = model.modelName

# set up run path
runId = datetime.datetime.now().isoformat()
experiment_dir = Path('experiments/' + args.experiment)
experiment_dir.mkdir(parents=True, exist_ok=True)
runPath = mkdtemp(prefix=runId, dir=str(experiment_dir))
sys.stdout = Logger('{}/run.log'.format(runPath))
print('Expt:', runPath)
print('RunID:', runId)

# save args to run
with open('{}/args.json'.format(runPath), 'w') as fp:
    json.dump(args.__dict__, fp)
# -- also save object because we want to recover these for other things
torch.save(args, '{}/args.rar'.format(runPath))

# preparation for training
optimizer = optim.Adam(filter(lambda p: p.requires_grad, model.parameters()),
                       lr=1e-3, amsgrad=True)
netE = _netE().cuda()
optE = optim.Adam(netE.parameters(), lr=0.0001, weight_decay=0, betas=(0.5, 0.999))

alphabet_path = os.path.join(os.getcwd(), 'alphabet.json')
with open(alphabet_path) as alphabet_file:
    alphabet = str(''.join(json.load(alphabet_file)))
#train = SVHNMNIST(alphabet,
#                  train=True,
#                  transform=transforms)
#test = SVHNMNIST(alphabet,
#                 train=False,
#                 transform=transforms)
#d_loader = DataLoader(train, batch_size=args.batch_size,
#                          shuffle=True, num_workers=8, drop_last=True);
train_loader, test_loader = model.getDataLoaders(args.batch_size, device=device)
objective = getattr(objectives,
                    ('m_' if hasattr(model, 'vaes') else '')
                    + args.obj
                    + ('_looser' if (args.looser and args.obj != 'elbo') else ''))
t_objective = getattr(objectives, ('m_' if hasattr(model, 'vaes') else '') + 'iwae')

e_l_step_size = 0.4
e_l_steps = 30
e_prior_sig = 1
def sample_langevin_prior_z(z, netE, verbose=False, noise=True):
    z = z.clone().detach()
    z.requires_grad = True
    for i in range(e_l_steps):
        en = energy(netE(z))
        z_grad = torch.autograd.grad(en.sum(), z)[0]

        z.data = z.data - 0.5 * e_l_step_size * e_l_step_size * (
                    z_grad + 1.0 / (e_prior_sig * e_prior_sig) * z.data)
        if noise:
            z.data += e_l_step_size * torch.randn_like(z).data

        #       if (i % 5 == 0 or i == args.e_l_steps - 1):
        #            print('Langevin prior {:3d}/{:3d}: energy={:8.3f}'.format(i+1, args.e_l_steps, en.sum().item()))

      #  z_grad_norm = z_grad.view(args.batch_size, -1).norm(dim=1).mean()

    return z.detach()

e_energy_form = 'identity'
def energy(score):
    if e_energy_form == 'tanh':
        energy = F.tanh(-score.squeeze())
    elif e_energy_form == 'sigmoid':
        energy = F.sigmoid(score.squeeze())
    elif e_energy_form == 'identity':
        energy = score.squeeze()
    elif e_energy_form == 'softplus':
        energy = F.softplus(score.squeeze())
    return energy

def train(epoch, agg):
    model.train()
    b_loss = 0
    po = 0
    ne = 0
    for i, dataT in enumerate(train_loader):
        data = unpack_data(dataT, device=device)
        optimizer.zero_grad()
        loss, z_g_k = objective(model, data, K=args.K)
        loss = -objective(model, data, K=args.K)

        loss += energy(netE(z_g_k.detach())).mean()
        loss.backward()
        optimizer.step()

        z_e_0 = model.pz(*model.pz_params).sample([data[0].shape[0]]).cuda()
        optE.zero_grad()
        z_e_k = sample_langevin_prior_z(z_e_0, netE, verbose=(i == 0))
        en_neg = energy(netE(z_e_k.detach())).mean()
        en_pos = energy(netE(z_g_k.detach())).mean()
        loss_e = en_pos - en_neg
        loss_e.backward()
        optE.step()

        po += en_pos.item()
        ne += en_neg.item()
        b_loss += loss.item()
        if args.print_freq > 0 and i % args.print_freq == 0:
            print("iteration {:04d}: loss: {:6.3f}".format(i, loss.item() / args.batch_size))
            print("iteration {:04d}: loss: {:6.3f}".format(i, loss_e.item() / args.batch_size))
    agg['train_loss'].append(b_loss / len(train_loader.dataset))
    agg['Pos'].append(po / len(train_loader.dataset))
    agg['Neg'].append(ne / len(train_loader.dataset))
    print('====> Epoch: {:03d} Train loss: {:.4f}'.format(epoch, agg['train_loss'][-1]))
    print('====> Epoch: {:03d} Pos loss: {:.4f}'.format(epoch, agg['Pos'][-1]))
    print('====> Epoch: {:03d} Neg loss: {:.4f}'.format(epoch, agg['Neg'][-1]))


def test(epoch, agg):
    model.eval()
    b_loss = 0
    with torch.no_grad():
        for i, dataT in enumerate(test_loader):
            data = unpack_data(dataT, device=device)
            loss = -t_objective(model, data, K=args.K)
            b_loss += loss.item()
            if i == 0:
                model.reconstruct(data, runPath, epoch)
                if not args.no_analytics:
                    model.analyse(data, runPath, epoch)
    agg['test_loss'].append(b_loss / len(test_loader.dataset))
    print('====>             Test loss: {:.4f}'.format(agg['test_loss'][-1]))


def estimate_log_marginal(K):
    """Compute an IWAE estimate of the log-marginal likelihood of test data."""
    model.eval()
    marginal_loglik = 0
    with torch.no_grad():
        for dataT in test_loader:
            data = unpack_data(dataT, device=device)
            marginal_loglik += -t_objective(model, data, K).item()

    marginal_loglik /= len(test_loader.dataset)
    print('Marginal Log Likelihood (IWAE, K = {}): {:.4f}'.format(K, marginal_loglik))


if __name__ == '__main__':

    with Timer('MM-VAE') as t:
        agg = defaultdict(list)
        for epoch in range(1, args.epochs + 1):
            train(epoch, agg)
            test(epoch, agg)
            save_model(model, runPath + '/model.rar')
            save_vars(agg, runPath + '/losses.rar')
            model.generate(runPath, epoch)
        if args.logp:  # compute as tight a marginal likelihood as possible
            estimate_log_marginal(5000)
