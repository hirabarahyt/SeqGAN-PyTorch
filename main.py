import argparse
import pickle as pkl

import torch
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn

from data_iter import DisDataIter, GenDataIter
from generator import Generator
from discriminator import Discriminator
from target_lstm import TargetLSTM
from rollout import Rollout
from loss import PGLoss
import random

# Arguemnts
parser = argparse.ArgumentParser(description='SeqGAN')
parser.add_argument('--hpc', action='store_true', default=False,
                    help='set to hpc mode')
parser.add_argument('--data_path', type=str, default='/scratch/zc807/seq_gan/', metavar='PATH',
                    help='data path to save files (default: /scratch/zc807/seq_gan/)')
parser.add_argument('--rounds', type=int, default=150, metavar='N',
                    help='rounds of adversarial training (default: 150)')
parser.add_argument('--g_pretrain_steps', type=int, default=5, metavar='N',
                    help='steps of pre-training of generators (default: 120)')
parser.add_argument('--d_pretrain_steps', type=int, default=20, metavar='N',
                    help='steps of pre-training of discriminators (default: 50)')
parser.add_argument('--g_steps', type=int, default=1, metavar='N',
                    help='steps of generator updates in one round of adverarial training (default: 1)')
parser.add_argument('--d_steps', type=int, default=3, metavar='N',
                    help='steps of discriminator updates in one round of adverarial training (default: 3)')
parser.add_argument('--gk_epochs', type=int, default=1, metavar='N',
                    help='epochs of generator updates in one step of generate update (default: 1)')
parser.add_argument('--dk_epochs', type=int, default=3, metavar='N',
                    help='epochs of discriminator updates in one step of discriminator update (default: 3)')
parser.add_argument('--update_rate', type=float, default=0.8, metavar='UR',
                    help='update rate of roll-out model (default: 0.8)')
parser.add_argument('--n_rollout', type=int, default=16, metavar='N',
                    help='number of roll-out (default: 16)')
parser.add_argument('--vocab_size', type=int, default=24, metavar='N',
                    help='vocabulary size (default: 10)')
parser.add_argument('--batch_size', type=int, default=64, metavar='N',
                    help='batch size (default: 64)')
parser.add_argument('--n_samples', type=int, default=64000, metavar='N',
                    help='number of samples gerenated per time (default: 6400)')
parser.add_argument('--gen_lr', type=float, default=1e-3, metavar='LR',
                    help='learning rate of generator optimizer (default: 1e-3)')
parser.add_argument('--dis_lr', type=float, default=1e-3, metavar='LR',
                    help='learning rate of discriminator optimizer (default: 1e-3)')
parser.add_argument('--no_cuda', action='store_true', default=False,
                    help='disables CUDA training')
parser.add_argument('--seed', type=int, default=1, metavar='S',
                    help='random seed (default: 1)')


pre_train = False

# Files
POSITIVE_FILE = 'transformed_real_data.txt'
# POSITIVE_FILE = 'real.data'
NEGATIVE_FILE = 'gene.data'
groundtruth_file = "groundtruth.txt"
sample_positive_file = "sampled_real.txt"


# Genrator Parameters
g_embed_dim = 64
g_hidden_dim = 64
g_seq_len = 32


# Discriminator Parameters
d_num_class = 2
d_embed_dim = 64
d_filter_sizes = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20]
d_num_filters = [100, 200, 200, 200, 200, 100, 100, 100, 100, 100, 160, 160]
d_dropout_prob = 0.2

def sample_real(real_file, out_file):
    oup = open(out_file, 'w')
    random_idx = random.randint(0,850000)
    end = random_idx + 64000
    with open(real_file, 'r') as f:
        n = 0
        for line in f:
            n += 1
            if n < random_idx:
                continue
            if n > end:
                continue
            oup.write(line)
    oup.close()

def eva_G(eva_iter, gen):
    top1 = 0
    total_num = 0
    for eva_data, eva_target in eva_iter:
        if args.cuda:
            eva_data, eva_target = eva_data.cuda(), eva_target.cuda()
        eva_target = eva_target.contiguous().view(-1)
        eva_output = gen(eva_data)
        _,maxk = torch.topk(eva_output, 1, dim=-1)
        maxk = maxk[:,0]
        top1 += (eva_target == maxk).sum().float()
        total_num += eva_target.shape[0]
    eva_iter.reset()
    print("eva acc: {:.5f}".format(top1/total_num))


def generate_samples(model, batch_size, generated_num, output_file):
    samples = []
    for _ in range(int(generated_num / batch_size)):
        sample = model.sample(batch_size, g_seq_len).cpu().data.numpy().tolist()
        samples.extend(sample)
    with open(output_file, 'w') as fout:
        for sample in samples:
            string = ' '.join([str(s) for s in sample])
            fout.write('{}\n'.format(string))


def train_generator_MLE(gen, data_iter, eva_iter, criterion, optimizer, epochs, 
        gen_pretrain_train_loss, args):
    """
    Train generator with MLE
    """
    show_every = 100
    for epoch in range(epochs):
        total_loss = 0.
        count = 0
        for data, target in data_iter:
            if args.cuda:
                data, target = data.cuda(), target.cuda()
            target = target.contiguous().view(-1)
            output = gen(data)
            loss = criterion(output, target)
            total_loss += loss.item()
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            if count % show_every == 0 and count != 0:
                avg_loss = total_loss / 100
                print("iteration {}, train loss: {:.5f}".format(count+epoch*len(data_iter), avg_loss))
                total_loss = 0
            count += 1
        data_iter.reset()

        eva_G(eva_iter, gen)
        # top1 = 0
        # total_num = 0
        # for eva_data, eva_target in eva_iter:
        #     if args.cuda:
        #         eva_data, eva_target = eva_data.cuda(), eva_target.cuda()
        #     eva_target = eva_target.contiguous().view(-1)
        #     eva_output = gen(eva_data)
        #     _,maxk = torch.topk(eva_output, 1, dim=-1)
        #     maxk = maxk[:,0]
        #     top1 += (eva_target == maxk).sum().float()
        #     total_num += eva_target.shape[0]
        # eva_iter.reset()
        # print("eva acc: {:.5f}".format(top1/total_num))



    


def train_generator_PG(gen, dis, rollout, pg_loss, optimizer, epochs, args):
    """
    Train generator with the guidance of policy gradient
    """
    for epoch in range(epochs):
        # construct the input to the genrator, add zeros before samples and delete the last column
        samples = generator.sample(args.batch_size, g_seq_len)
        zeros = torch.zeros(args.batch_size, 1, dtype=torch.int64)
        if samples.is_cuda:
            zeros = zeros.cuda()
        inputs = torch.cat([zeros, samples.data], dim = 1)[:, :-1].contiguous()
        targets = samples.data.contiguous().view((-1,))

        # calculate the reward
        rewards = torch.tensor(rollout.get_reward(samples, args.n_rollout, dis))
        if args.cuda:
            rewards = rewards.cuda()

        # update generator
        output = gen(inputs)
        loss = pg_loss(output, targets, rewards)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()


def eval_generator(model, data_iter, criterion, args):
    """
    Evaluate generator with NLL
    """
    total_loss = 0.
    with torch.no_grad():
        for data, target in data_iter:
            if args.cuda:
                data, target = data.cuda(), target.cuda()
            target = target.contiguous().view(-1)
            pred = model(data)
            loss = criterion(pred, target)
            total_loss += loss.item()
    avg_loss = total_loss / len(data_iter)
    return avg_loss


def train_discriminator(dis, gen, criterion, optimizer, epochs, 
        dis_adversarial_train_loss, dis_adversarial_train_acc, args):
    """
    Train discriminator
    """
    generate_samples(gen, args.batch_size, args.n_samples, NEGATIVE_FILE)
    # data_iter = DisDataIter(POSITIVE_FILE, NEGATIVE_FILE, args.batch_size)
    sample_real(POSITIVE_FILE, sample_positive_file)
    data_iter = DisDataIter(sample_positive_file, NEGATIVE_FILE, args.batch_size)
    for epoch in range(epochs):
        correct = 0
        total_loss = 0.
        for data, target in data_iter:
            if args.cuda:
                data, target = data.cuda(), target.cuda()
            target = target.contiguous().view(-1)
            output = dis(data)
            pred = output.data.max(1)[1]
            correct += pred.eq(target.data).cpu().sum()
            loss = criterion(output, target)
            total_loss += loss.item()
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        data_iter.reset()
        avg_loss = total_loss / len(data_iter)
        acc = correct.item() / data_iter.data_num
        print("Epoch {}, train loss: {:.5f}, train acc: {:.3f}".format(epoch, avg_loss, acc))
        dis_adversarial_train_loss.append(avg_loss)
        dis_adversarial_train_acc.append(acc)


def eval_discriminator(model, data_iter, criterion, args):
    """
    Evaluate discriminator, dropout is enabled
    """
    correct = 0
    total_loss = 0.
    with torch.no_grad():
        for data, target in data_iter:
            if args.cuda:
                data, target = data.cuda(), target.cuda()
            target = target.contiguous().view(-1)
            output = model(data)
            pred = output.data.max(1)[1]
            correct += pred.eq(target.data).cpu().sum()
            loss = criterion(output, target)
            total_loss += loss.item()
    avg_loss = total_loss / len(data_iter)
    acc = correct.item() / data_iter.data_num
    return avg_loss, acc


def adversarial_train(gen, dis, rollout, pg_loss, nll_loss, gen_optimizer, dis_optimizer, 
        dis_adversarial_train_loss, dis_adversarial_train_acc, args):
    """
    Adversarially train generator and discriminator
    """
    # train generator for g_steps
    print("#Train generator")
    for i in range(args.g_steps):
        print("##G-Step {}".format(i))
        train_generator_PG(gen, dis, rollout, pg_loss, gen_optimizer, args.gk_epochs, args)

    # train discriminator for d_steps
    print("#Train discriminator")
    for i in range(args.d_steps):
        print("##D-Step {}".format(i))
        train_discriminator(dis, gen, nll_loss, dis_optimizer, args.dk_epochs, 
            dis_adversarial_train_loss, dis_adversarial_train_acc, args)

    # update roll-out model
    rollout.update_params()

def load_weight(model, weight_path, eva=False):
    if torch.cuda.is_available():
        para = torch.load(weight_path)
        # para = torch.load(weight_path, map_location={'cuda:2':'cuda:0'})
    else:
        para = torch.load(weight_path, map_location='cpu')
    model.load_state_dict(para)
    if torch.cuda.is_available():
        model.cuda()
    if eva:
        model.eval()
    return model


if __name__ == '__main__':
    # Parse arguments
    args = parser.parse_args()
    args.cuda = not args.no_cuda and torch.cuda.is_available()
    torch.manual_seed(args.seed)
    if args.cuda:
        torch.cuda.manual_seed(args.seed)
    if not args.hpc:
        args.data_path = ''
    POSITIVE_FILE = args.data_path + POSITIVE_FILE
    NEGATIVE_FILE = args.data_path + NEGATIVE_FILE

    # Set models, criteria, optimizers
    generator = Generator(args.vocab_size, g_embed_dim, g_hidden_dim, args.cuda)
    discriminator = Discriminator(d_num_class, args.vocab_size, d_embed_dim, d_filter_sizes, d_num_filters, d_dropout_prob)
    target_lstm = TargetLSTM(args.vocab_size, g_embed_dim, g_hidden_dim, args.cuda)
    nll_loss = nn.NLLLoss()
    pg_loss = PGLoss()
    if args.cuda:
        generator = generator.cuda()
        discriminator = discriminator.cuda()
        target_lstm = target_lstm.cuda()
        nll_loss = nll_loss.cuda()
        pg_loss = pg_loss.cuda()
        cudnn.benchmark = True
    gen_optimizer = optim.Adam(params=generator.parameters(), lr=args.gen_lr)
    dis_optimizer = optim.SGD(params=discriminator.parameters(), lr=args.dis_lr)

    # Container of experiment data
    gen_pretrain_train_loss = []
    gen_pretrain_eval_loss = []
    dis_pretrain_train_loss = []
    dis_pretrain_train_acc = []
    dis_pretrain_eval_loss = []
    dis_pretrain_eval_acc = []
    gen_adversarial_eval_loss = []
    dis_adversarial_train_loss = []
    dis_adversarial_train_acc = []
    dis_adversarial_eval_loss = []
    dis_adversarial_eval_acc = []

    #Generate toy data using target LSTM
    # print('#####################################################')
    # print('Generating data ...')
    # print('#####################################################\n\n')
    # generate_samples(target_lstm, args.batch_size, args.n_samples, POSITIVE_FILE)

    if pre_train:
        # Pre-train generator using MLE
        print('#####################################################')
        print('Start pre-training generator with MLE...')
        print('#####################################################\n')
        gen_data_iter = GenDataIter(POSITIVE_FILE, args.batch_size)
        eva_data_iter = GenDataIter(groundtruth_file, args.batch_size)
        for i in range(args.g_pretrain_steps):
            print("G-Step {}".format(i))
            train_generator_MLE(generator, gen_data_iter, eva_data_iter, nll_loss, 
                gen_optimizer, args.gk_epochs, gen_pretrain_train_loss, args)
            # generate_samples(generator, args.batch_size, args.n_samples, NEGATIVE_FILE)
            # eval_iter = GenDataIter(NEGATIVE_FILE, args.batch_size)
            # gen_loss = eval_generator(target_lstm, eval_iter, nll_loss, args)
            # gen_pretrain_eval_loss.append(gen_loss)
            # print("eval loss: {:.5f}\n".format(gen_loss))
        torch.save(generator.state_dict(),"checkpoints/preG.pth".format(i))
        print('#####################################################\n\n')

        # Pre-train discriminator
        print('#####################################################')
        print('Start pre-training discriminator...')
        print('#####################################################\n')
        for i in range(args.d_pretrain_steps):
            print("D-Step {}".format(i))
            train_discriminator(discriminator, generator, nll_loss, 
                dis_optimizer, args.dk_epochs, dis_adversarial_train_loss, dis_adversarial_train_acc, args)
            generate_samples(generator, args.batch_size, args.n_samples, NEGATIVE_FILE)
            sample_real(POSITIVE_FILE, sample_positive_file)
            eval_iter = DisDataIter(sample_positive_file, NEGATIVE_FILE, args.batch_size)
            # eval_iter = DisDataIter(POSITIVE_FILE, NEGATIVE_FILE, args.batch_size)
            dis_loss, dis_acc = eval_discriminator(discriminator, eval_iter, nll_loss, args)
            dis_pretrain_eval_loss.append(dis_loss)
            dis_pretrain_eval_acc.append(dis_acc)
            print("eval loss: {:.5f}, eval acc: {:.3f}\n".format(dis_loss, dis_acc))
        torch.save(discriminator.state_dict(),"checkpoints/preD.pth".format(i))
        print('#####################################################\n\n')

    else:
        generator = load_weight(generator, "checkpoints/preG.pth")
        discriminator = load_weight(discriminator, "checkpoints/preD.pth")

    eva_data_iter = GenDataIter(groundtruth_file, args.batch_size)
    eva_G(eva_data_iter, generator)

    # Adversarial training
    print('#####################################################')
    print('Start adversarial training...')
    print('#####################################################\n')
    rollout = Rollout(generator, args.update_rate)
    for i in range(args.rounds):
        print("Round {}".format(i))
        adversarial_train(generator, discriminator, rollout, 
            pg_loss, nll_loss, gen_optimizer, dis_optimizer, 
            dis_adversarial_train_loss, dis_adversarial_train_acc, args)

        eva_data_iter = GenDataIter(groundtruth_file, args.batch_size)
        eva_G(eva_data_iter, generator)
        torch.save(generator.state_dict(),"checkpoints/ckG_{}.pth".format(i))
        torch.save(discriminator.state_dict(),"checkpoints/ckD_{}.pth".format(i))
        # generate_samples(generator, args.batch_size, args.n_samples, NEGATIVE_FILE)
        # gen_eval_iter = GenDataIter(NEGATIVE_FILE, args.batch_size)
        # dis_eval_iter = DisDataIter(POSITIVE_FILE, NEGATIVE_FILE, args.batch_size)
        # gen_loss = eval_generator(target_lstm, gen_eval_iter, nll_loss, args)
        # gen_adversarial_eval_loss.append(gen_loss)
        # dis_loss, dis_acc = eval_discriminator(discriminator, dis_eval_iter, nll_loss, args)
        # dis_adversarial_eval_loss.append(dis_loss)
        # dis_adversarial_eval_acc.append(dis_acc)
        # print("gen eval loss: {:.5f}, dis eval loss: {:.5f}, dis eval acc: {:.3f}\n"
        #     .format(gen_loss, dis_loss, dis_acc))

    # Save experiment data
    # with open(args.data_path + 'experiment.pkl', 'wb') as f:
    #     pkl.dump(
    #         (gen_pretrain_train_loss,
    #             gen_pretrain_eval_loss,
    #             dis_pretrain_train_loss,
    #             dis_pretrain_train_acc,
    #             dis_pretrain_eval_loss,
    #             dis_pretrain_eval_acc,
    #             gen_adversarial_eval_loss,
    #             dis_adversarial_train_loss,
    #             dis_adversarial_train_acc,
    #             dis_adversarial_eval_loss,
    #             dis_adversarial_eval_acc),
    #         f,
    #         protocol=pkl.HIGHEST_PROTOCOL
    #     )
