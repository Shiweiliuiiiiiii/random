from __future__ import print_function
import torch
import torch.nn as nn
import torch.optim as optim
from sparselearning.snip import SNIP, GraSP
import numpy as np
import math



class CosineDecay(object):
    def __init__(self, death_rate, T_max, eta_min=0.005, last_epoch=-1):
        self.sgd = optim.SGD(torch.nn.ParameterList([torch.nn.Parameter(torch.zeros(1))]), lr=death_rate)
        self.cosine_stepper = torch.optim.lr_scheduler.CosineAnnealingLR(self.sgd, T_max, eta_min, last_epoch)
        self.T_max = T_max
    def step(self):
        self.cosine_stepper.step()

    def get_dr(self):
        return self.sgd.param_groups[0]['lr']

class LinearDecay(object):
    def __init__(self, death_rate, factor=0.99, frequency=600):
        self.factor = factor
        self.steps = 0
        self.frequency = frequency

    def step(self):
        self.steps += 1

    def get_dr(self, death_rate):
        if self.steps > 0 and self.steps % self.frequency == 0:
            return death_rate*self.factor
        else:
            return death_rate


class Masking(object):
    def __init__(self, optimizer, death_rate=0.3, growth_death_ratio=1.0, death_rate_decay=None, death_mode='magnitude', growth_mode='momentum', redistribution_mode='momentum', threshold=0.001, args=False, train_loader=False):
        growth_modes = ['random', 'momentum', 'momentum_neuron', 'gradient']
        if growth_mode not in growth_modes:
            print('Growth mode: {0} not supported!'.format(growth_mode))
            print('Supported modes are:', str(growth_modes))

        self.train_loader = train_loader
        self.args = args
        self.device = torch.device("cuda")
        self.growth_mode = growth_mode
        self.death_mode = death_mode
        self.growth_death_ratio = growth_death_ratio
        self.redistribution_mode = redistribution_mode
        self.death_rate_decay = death_rate_decay

        self.sparse_mode = args.sparse_mode

        self.total_step = self.death_rate_decay.T_max
        self.final_prune_time = int(self.total_step * args.final_prune_time)
        self.initial_prune_time = int(self.total_step * args.initial_prune_time)

        self.masks = {}
        self.modules = []
        self.names = []
        self.optimizer = optimizer

        # stats
        self.name2zeros = {}
        self.num_remove = {}
        self.name2nonzeros = {}
        self.death_rate = death_rate
        self.steps = 0

        # if fix, then we do not explore the sparse connectivity
        if args.fix: self.update_frequency = None
        else: self.update_frequency = args.update_frequency

    def init(self, mode='ERK', density=0.05, erk_power_scale=1.0):
        self.density = density

        if mode == 'dense':
            print('initialized with dense model')
            self.baseline_nonzero = 0
            for module in self.modules:
                for name, weight in module.named_parameters():
                    if name not in self.masks: continue
                    self.masks[name] = torch.ones_like(weight, dtype=torch.float32, requires_grad=False).to('cuda')

        elif mode == 'one_shot_gm':
            print('initialize by one_shot_gm')
            self.baseline_nonzero = 0

            weight_abs = []
            for module in self.modules:
                for name, weight in module.named_parameters():
                    if name not in self.masks: continue
                    weight_abs.append(torch.abs(weight))

            # Gather all scores in a single vector and normalise
            all_scores = torch.cat([torch.flatten(x) for x in weight_abs])
            num_params_to_keep = int(len(all_scores) * density)

            threshold, _ = torch.topk(all_scores, num_params_to_keep, sorted=True)
            acceptable_score = threshold[-1]

            for module in self.modules:
                for name, weight in module.named_parameters():
                    if name not in self.masks: continue
                    self.masks[name] = ((torch.abs(weight)) > acceptable_score).float().data.to('cuda')

        elif mode == 'one_shot_gm_cpu':
            print('initialize by one_shot_gm')
            self.baseline_nonzero = 0

            weight_abs = []
            for module in self.modules:
                for name, weight in module.named_parameters():
                    if name not in self.masks: continue
                    weight_abs.append(torch.abs(weight.cpu()))

            # Gather all scores in a single vector and normalise
            all_scores = torch.cat([torch.flatten(x) for x in weight_abs])
            num_params_to_keep = int(len(all_scores) * density)

            threshold, _ = torch.topk(all_scores, num_params_to_keep, sorted=True)
            acceptable_score = threshold[-1]

            for module in self.modules:
                for name, weight in module.named_parameters():
                    if name not in self.masks: continue
                    self.masks[name] = ((torch.abs(weight)) > acceptable_score).float().data.to('cuda')

        elif mode == 'random':
            print('initialize by random pruning')
            self.baseline_nonzero = 0
            for module in self.modules:
                for name, weight in module.named_parameters():
                    if name not in self.masks: continue
                    self.masks[name] = (torch.rand(weight.shape) < density).float().data.to('cuda')

        if mode == 'iterative_gm':
            print('initialized by iterative_gm')
            total_num_nonzoros = 0
            dense_nonzeros = 0
            for module in self.modules:
                for name, weight in module.named_parameters():
                    if name not in self.masks: continue
                    self.masks[name] = (weight != 0).cuda()
                    self.name2nonzeros[name] = (weight != 0).sum().item()
                    total_num_nonzoros += self.name2nonzeros[name]
                    dense_nonzeros += weight.numel()
                    print(f'sparsity of layer {name} is {self.name2nonzeros[name] / weight.numel()}')

            print(f'sparsity level of current model is {1 - total_num_nonzoros / dense_nonzeros}')

            weight_abs = []
            for module in self.modules:
                for name, weight in module.named_parameters():
                    if name not in self.masks: continue
                    weight_abs.append(torch.abs(weight))

            # Gather all scores in a single vector and normalise
            all_scores = torch.cat([torch.flatten(x) for x in weight_abs])
            num_params_to_keep = int(total_num_nonzoros * density)

            threshold, _ = torch.topk(all_scores, num_params_to_keep, sorted=True)
            acceptable_score = threshold[-1]

            for module in self.modules:
                for name, weight in module.named_parameters():
                    if name not in self.masks: continue
                    self.masks[name] = ((torch.abs(weight)) > acceptable_score).float().data.to('cuda')


        elif mode == 'snip':
            print('initialize by snip')
            layer_wise_sparsities = SNIP(self.module, self.density, self.train_loader, self.device)
            # re-sample mask positions
            for sparsity_, name in zip(layer_wise_sparsities, self.masks):
                self.masks[name][:] = (torch.rand(self.masks[name].shape) < (1-sparsity_)).float().data.cuda()

        elif mode == 'GraSP':
            print('initialize by GraSP')
            layer_wise_sparsities = GraSP(self.module, self.density, self.train_loader, self.device)
            # re-sample mask positions
            for sparsity_, name in zip(layer_wise_sparsities, self.masks):
                self.masks[name][:] = (torch.rand(self.masks[name].shape) < (1-sparsity_)).float().data.cuda()

        self.apply_mask()

        total_size = 0
        sparse_size = 0
        for module in self.modules:
            for name, weight in module.named_parameters():
                if name in self.masks:
                    print(name, 'density:',(weight!=0).sum().item()/weight.numel())
                    total_size += weight.numel()
                    sparse_size += (weight != 0).sum().int().item()
        print('Total Model parameters:', total_size)
        print('Total parameters under sparsity level of {0}: {1}'.format(self.density, sparse_size / total_size))

        # self.fired_masks = copy.deepcopy(self.masks) # used for ITOP
        # self.print_nonzero_counts()


    def step(self):
        self.optimizer.step()
        self.apply_mask()
        self.death_rate_decay.step()
        self.death_rate = self.death_rate_decay.get_dr()
        self.steps += 1

        if self.update_frequency is not None:
            if self.sparse_mode == 'GMP':
                if self.steps >= self.initial_prune_time and self.steps < self.final_prune_time and self.steps % self.update_frequency == 0:
                    print('*********************************Gradual Magnitude Pruning***********************')
                    current_prune_rate = self.gradual_pruning_rate(self.steps, 0.0, 1-self.density,
                                                                   self.initial_prune_time, self.final_prune_time)
                    self.gradual_magnitude_pruning(current_prune_rate)
                    self.print_status()

            elif self.sparse_mode == 'GMP_cpu':
                if self.steps >= self.initial_prune_time and self.steps < self.final_prune_time and self.steps % self.update_frequency == 0:
                    print('*********************************Gradual Magnitude Pruning***********************')
                    current_prune_rate = self.gradual_pruning_rate(self.steps, 0.0, 1-self.density,
                                                                   self.initial_prune_time, self.final_prune_time)
                    self.gradual_magnitude_pruning(current_prune_rate, True)
                    self.print_status()

            elif self.sparse_mode == 'oBERT':
                if self.steps >= self.initial_prune_time and self.steps < self.final_prune_time and self.steps % self.update_frequency == 0:
                    print('*********************************Gradual oBERT Pruning***********************')
                    current_prune_rate = self.gradual_pruning_rate(self.steps, 0.0, 1-self.density,
                                                                   self.initial_prune_time, self.final_prune_time)
                    self.gradual_oBERT_pruning(current_prune_rate)
                    self.print_status()

            elif self.sparse_mode == 'DST':
                if self.steps % self.update_frequency == 0:
                    print('*********************************Dynamic Sparsity********************************')
                    self.truncate_weights()
                    self.print_nonzero_counts()
            else:
                pass
                # self.print_nonzero_counts()


    def add_module(self, module, density, sparse_init='ER'):
        self.modules.append(module)
        self.module = module
        for name, tensor in module.named_parameters():
            self.names.append(name)
            self.masks[name] = torch.ones_like(tensor, dtype=torch.float32, requires_grad=False).cuda()

        print('Removing biases...')
        self.remove_weight_partial_name('bias')
        print('Removing 2D batch norms...')
        self.remove_type(nn.BatchNorm2d)
        print('Removing 1D batch norms...')
        self.remove_type(nn.BatchNorm1d)
        self.init(mode=sparse_init, density=density)


    def remove_weight(self, name):
        if name in self.masks:
            print('Removing {0} of size {1} = {2} parameters.'.format(name, self.masks[name].shape,
                                                                      self.masks[name].numel()))
            self.masks.pop(name)
        elif name + '.weight' in self.masks:
            print('Removing {0} of size {1} = {2} parameters.'.format(name, self.masks[name + '.weight'].shape,
                                                                      self.masks[name + '.weight'].numel()))
            self.masks.pop(name + '.weight')
        else:
            print('ERROR', name)

    def remove_weight_partial_name(self, partial_name):
        removed = set()
        for name in list(self.masks.keys()):
            if partial_name in name:

                print('Removing {0} of size {1} with {2} parameters...'.format(name, self.masks[name].shape,
                                                                                   np.prod(self.masks[name].shape)))
                removed.add(name)
                self.masks.pop(name)

        print('Removed {0} layers.'.format(len(removed)))

        i = 0
        while i < len(self.names):
            name = self.names[i]
            if name in removed:
                self.names.pop(i)
            else:
                i += 1

    def remove_type(self, nn_type):
        for module in self.modules:
            for name, module in module.named_modules():
                if isinstance(module, nn_type):
                    self.remove_weight(name)

    def apply_mask(self):
        for module in self.modules:
            for name, tensor in module.named_parameters():
                if name in self.masks:
                    tensor.data = tensor.data*self.masks[name]

    def truncate_weights(self):

        for module in self.modules:
            for name, weight in module.named_parameters():
                if name not in self.masks: continue
                mask = self.masks[name]
                self.name2nonzeros[name] = mask.sum().item()
                self.name2zeros[name] = mask.numel() - self.name2nonzeros[name]

                # death
                if self.death_mode == 'magnitude':
                    new_mask = self.magnitude_death(mask, weight, name)
                elif self.death_mode == 'SET':
                    new_mask = self.magnitude_and_negativity_death(mask, weight, name)
                elif self.death_mode == 'Taylor_FO':
                    new_mask = self.taylor_FO(mask, weight, name)
                elif self.death_mode == 'threshold':
                    new_mask = self.threshold_death(mask, weight, name)

                self.num_remove[name] = int(self.name2nonzeros[name] - new_mask.sum().item())
                self.masks[name][:] = new_mask


        for module in self.modules:
            for name, weight in module.named_parameters():
                if name not in self.masks: continue
                new_mask = self.masks[name].data.byte()

                # growth
                if self.growth_mode == 'random':
                    new_mask = self.random_growth(name, new_mask, weight)

                if self.growth_mode == 'random_unfired':
                    new_mask = self.random_unfired_growth(name, new_mask, weight)

                elif self.growth_mode == 'momentum':
                    new_mask = self.momentum_growth(name, new_mask, weight)

                elif self.growth_mode == 'gradient':
                    new_mask = self.gradient_growth(name, new_mask, weight)

                new_nonzero = new_mask.sum().item()

                # exchanging masks
                self.masks.pop(name)
                self.masks[name] = new_mask.float()

        self.apply_mask()


    '''
                    DEATH
    '''

    def threshold_death(self, mask, weight, name):
        return (torch.abs(weight.data) > self.threshold)

    def taylor_FO(self, mask, weight, name):

        num_remove = math.ceil(self.death_rate * self.name2nonzeros[name])
        num_zeros = self.name2zeros[name]
        k = math.ceil(num_zeros + num_remove)

        x, idx = torch.sort((weight.data * weight.grad).pow(2).flatten())
        mask.data.view(-1)[idx[:k]] = 0.0

        return mask

    def magnitude_death(self, mask, weight, name):

        num_remove = math.ceil(self.death_rate*self.name2nonzeros[name])
        if num_remove == 0.0: return weight.data != 0.0
        num_zeros = self.name2zeros[name]

        x, idx = torch.sort(torch.abs(weight.data.view(-1)))
        n = idx.shape[0]

        k = math.ceil(num_zeros + num_remove)
        threshold = x[k-1].item()

        return (torch.abs(weight.data) > threshold)


    def magnitude_and_negativity_death(self, mask, weight, name):
        num_remove = math.ceil(self.death_rate*self.name2nonzeros[name])
        num_zeros = self.name2zeros[name]

        # find magnitude threshold
        # remove all weights which absolute value is smaller than threshold
        x, idx = torch.sort(weight[weight > 0.0].data.view(-1))
        k = math.ceil(num_remove/2.0)
        if k >= x.shape[0]:
            k = x.shape[0]

        threshold_magnitude = x[k-1].item()

        # find negativity threshold
        # remove all weights which are smaller than threshold
        x, idx = torch.sort(weight[weight < 0.0].view(-1))
        k = math.ceil(num_remove/2.0)
        if k >= x.shape[0]:
            k = x.shape[0]
        threshold_negativity = x[k-1].item()


        pos_mask = (weight.data > threshold_magnitude) & (weight.data > 0.0)
        neg_mask = (weight.data < threshold_negativity) & (weight.data < 0.0)


        new_mask = pos_mask | neg_mask
        return new_mask

    '''
                    GROWTH
    '''

    def random_unfired_growth(self, name, new_mask, weight):
        total_regrowth = self.num_remove[name]
        n = (new_mask == 0).sum().item()
        if n == 0: return new_mask
        num_nonfired_weights = (self.fired_masks[name]==0).sum().item()

        if total_regrowth <= num_nonfired_weights:
            idx = (self.fired_masks[name].flatten() == 0).nonzero()
            indices = torch.randperm(len(idx))[:total_regrowth]

            # idx = torch.nonzero(self.fired_masks[name].flatten())
            new_mask.data.view(-1)[idx[indices]] = 1.0
        else:
            new_mask[self.fired_masks[name]==0] = 1.0
            n = (new_mask == 0).sum().item()
            expeced_growth_probability = ((total_regrowth-num_nonfired_weights) / n)
            new_weights = torch.rand(new_mask.shape).cuda() < expeced_growth_probability
            new_mask = new_mask.byte() | new_weights
        return new_mask

    def random_growth(self, name, new_mask, weight):
        total_regrowth = self.num_remove[name]
        n = (new_mask==0).sum().item()
        if n == 0: return new_mask
        expeced_growth_probability = (total_regrowth/n)
        new_weights = torch.rand(new_mask.shape).cuda() < expeced_growth_probability
        new_mask_ = new_mask.byte() | new_weights
        if (new_mask_!=0).sum().item() == 0:
            new_mask_ = new_mask
        return new_mask_

    def momentum_growth(self, name, new_mask, weight):
        total_regrowth = self.num_remove[name]
        grad = self.get_momentum_for_weight(weight)
        grad = grad*(new_mask==0).float()
        y, idx = torch.sort(torch.abs(grad).flatten(), descending=True)
        new_mask.data.view(-1)[idx[:total_regrowth]] = 1.0

        return new_mask

    def gradient_growth(self, name, new_mask, weight):
        total_regrowth = self.num_remove[name]
        grad = self.get_gradient_for_weights(weight)
        grad = grad*(new_mask==0).float()

        y, idx = torch.sort(torch.abs(grad).flatten(), descending=True)
        new_mask.data.view(-1)[idx[:total_regrowth]] = 1.0

        return new_mask



    def momentum_neuron_growth(self, name, new_mask, weight):
        total_regrowth = self.num_remove[name]
        grad = self.get_momentum_for_weight(weight)

        M = torch.abs(grad)
        if len(M.shape) == 2: sum_dim = [1]
        elif len(M.shape) == 4: sum_dim = [1, 2, 3]

        v = M.mean(sum_dim).data
        v /= v.sum()

        slots_per_neuron = (new_mask==0).sum(sum_dim)

        M = M*(new_mask==0).float()
        for i, fraction  in enumerate(v):
            neuron_regrowth = math.floor(fraction.item()*total_regrowth)
            available = slots_per_neuron[i].item()

            y, idx = torch.sort(M[i].flatten())
            if neuron_regrowth > available:
                neuron_regrowth = available
            threshold = y[-(neuron_regrowth)].item()
            if threshold == 0.0: continue
            if neuron_regrowth < 10: continue
            new_mask[i] = new_mask[i] | (M[i] > threshold)

        return new_mask

    '''
                UTILITY
    '''
    def get_momentum_for_weight(self, weight):
        if 'exp_avg' in self.optimizer.state[weight]:
            adam_m1 = self.optimizer.state[weight]['exp_avg']
            adam_m2 = self.optimizer.state[weight]['exp_avg_sq']
            grad = adam_m1/(torch.sqrt(adam_m2) + 1e-08)
        elif 'momentum_buffer' in self.optimizer.state[weight]:
            grad = self.optimizer.state[weight]['momentum_buffer']
        return grad

    def get_gradient_for_weights(self, weight):
        grad = weight.grad.clone()
        return grad

    def print_nonzero_counts(self):
        for module in self.modules:
            for name, tensor in module.named_parameters():
                if name not in self.masks: continue
                mask = self.masks[name]
                num_nonzeros = (mask != 0).sum().item()
                val = '{0}: {1}->{2}, density: {3:.3f}'.format(name, self.name2nonzeros[name], num_nonzeros, num_nonzeros/float(mask.numel()))
                print(val)
        print('Prune rate: {0}\n'.format(self.death_rate))

    def fired_masks_update(self):
        ntotal_fired_weights = 0.0
        ntotal_weights = 0.0
        layer_fired_weights = {}
        for module in self.modules:
            for name, weight in module.named_parameters():
                if name not in self.masks: continue
                self.fired_masks[name] = self.masks[name].data.byte() | self.fired_masks[name].data.byte()
                ntotal_fired_weights += float(self.fired_masks[name].sum().item())
                ntotal_weights += float(self.fired_masks[name].numel())
                layer_fired_weights[name] = float(self.fired_masks[name].sum().item())/float(self.fired_masks[name].numel())
                print('Layerwise percentage of the fired weights of', name, 'is:', layer_fired_weights[name])
        total_fired_weights = ntotal_fired_weights/ntotal_weights
        print('The percentage of the total fired weights is:', total_fired_weights)
        return layer_fired_weights, total_fired_weights

    def gradual_pruning_rate(self,
            step: int,
            initial_threshold: float,
            final_threshold: float,
            initial_time: int,
            final_time: int,
    ):
        if step <= initial_time:
            threshold = initial_threshold
        elif step > final_time:
            threshold = final_threshold
        else:
            mul_coeff = 1 - (step - initial_time) / (final_time - initial_time)
            threshold = final_threshold + (initial_threshold - final_threshold) * (mul_coeff ** 3)

        return threshold

    def gradual_magnitude_pruning(self, current_pruning_rate, cpu=False):
        weight_abs = []
        for module in self.modules:
            for name, weight in module.named_parameters():
                if name not in self.masks: continue
                if cpu:
                    weight_abs.append(torch.abs(weight.cpu()))
                else:
                    weight_abs.append(torch.abs(weight))

        # Gather all scores in a single vector and normalise
        all_scores = torch.cat([torch.flatten(x) for x in weight_abs])
        num_params_to_keep = int(len(all_scores) * (1 - current_pruning_rate))

        threshold, _ = torch.topk(all_scores, num_params_to_keep, sorted=True)
        acceptable_score = threshold[-1]

        for module in self.modules:
            for name, weight in module.named_parameters():
                if name not in self.masks: continue
                self.masks[name] = ((torch.abs(weight)) > acceptable_score).float().data.to(self.device)
        self.apply_mask()

    def print_status(self):
        total_size = 0
        sparse_size = 0
        for module in self.modules:
            for name, weight in module.named_parameters():
                if name not in self.masks: continue
                dense_weight_num = weight.numel()
                sparse_weight_num = (weight != 0).sum().int().item()
                total_size += dense_weight_num
                sparse_size += sparse_weight_num
                layer_density = sparse_weight_num / dense_weight_num
                print(f'sparsity of layer {name} with tensor {weight.size()} is {1-layer_density}')
        print('Final sparsity level of {0}: {1}'.format(1-self.density, 1 - sparse_size / total_size))
