import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.cuda.amp import custom_fwd, custom_bwd
import random
import numpy as np
from itertools import zip_longest
import math
from dropbp.layer import DropBP
from dropbp.cpp_extention.allocate_p import allocate_p

class DropBPHandler:
    def __init__(self, model, drop_rate):
        self.model = model
        self.num_layers = sum(1 for module in self.model.modules() if isinstance(module, DropBP))
        self.p = drop_rate # target avg drop rate
        self.drop_rates=[drop_rate for _ in range(self.num_layers)] # drop rates list 
        
    def set_initial_drop_rate(self,p=None):
        for module in self.model.modules():
            if isinstance(module, DropBP):
                if p == None:
                    module.p = self.p
                else:
                    module.p = p
                
    def set_diverse_drop_rate(self, p_list, print_log=False):
        if len(p_list) != self.num_layers:
            raise ValueError(f"The number of drop rates ({len(p_list)}) does not match the number of DropBP layers ({self.num_layers}).")
        for i, module in enumerate(filter(lambda m: isinstance(m, DropBP), self.model.modules())):
            module.p = p_list[i]
            if print_log:
                print(f"Drop rate of layer {i+1} is set to {p_list[i]}")

    def sensitivity_based_drop_bp(self, backprop, target_probability=0.5, min_rate=0, gradnorm=True):
        init_p = [min_rate]*self.num_layers
        sensitivities=self.compute_sensitivity(backprop, gradnorm=gradnorm)
        flops = self.extract_flops()
        target_flops = int((1-target_probability)*sum(flops))
        
        drop_rates= allocate_p(torch.tensor(init_p, dtype=torch.float),
                            torch.tensor(sensitivities, dtype=torch.float),
                            torch.tensor(flops, dtype=torch.float),
                            target_flops,
                            min_rate)
        drop_rates = drop_rates.cpu().numpy()
        self.drop_rates=drop_rates
        print(drop_rates)
        
        self.set_diverse_drop_rate(drop_rates)
        return sensitivities, drop_rates
    
    def set_dropped_layers(self, ):
        # set the dropped layers per iteration
        # this code forcibly adjusts the number of dropped layers to match the target average drop rate 
        # if the total number of dropped layers is lower than the target average drop rate.
        dropped_layers = [1 if np.random.rand() < drop_rate else 0 for drop_rate in self.drop_rates]
        n_dropped_attn = sum(dropped_layers[i] for i in range(len(dropped_layers)) if i % 2 == 1)
        n_dropped_ffn = sum(dropped_layers[i] for i in range(len(dropped_layers)) if i % 2 == 0)
        current_avg_drop_rate = (n_dropped_attn+n_dropped_ffn) / self.num_layers
        
        if current_avg_drop_rate < self.p:
            sorted_indices = sorted(range(self.num_layers), key=lambda i: self.drop_rates[i], reverse=True)
            for i in sorted_indices:
                if current_avg_drop_rate >= self.p:
                    break
                if dropped_layers[i] == 0:
                    dropped_layers[i] = 1
                    if i % 2 == 1:
                        n_dropped_attn += 1
                    else:
                        n_dropped_ffn += 1
                    current_avg_drop_rate = current_avg_drop_rate = (n_dropped_attn+n_dropped_ffn) / (self.num_layers)
        self.set_diverse_drop_rate(dropped_layers, print_log=False)

    def compute_gradient(self, backprop, gradnorm=True, seed=526):
        setup_seed(seed)
        backprop()
        grad = []
        for p in self.model.parameters():
            if p.grad is not None:
                grad.append(p.grad.norm() if gradnorm else p.grad)
            else:
                grad.append(0)
        return grad

    def compute_sensitivity(self, backprop, gradnorm=True):
        sensitivities = []
        self.set_initial_drop_rate(0)
        original_grad = self.compute_gradient(backprop, gradnorm)
        for i in range(self.num_layers):
            self.set_diverse_drop_rate([1 if j == i else 0 for j in range(self.num_layers)])
            new_grad = self.compute_gradient(backprop, gradnorm)
            sensitivity = sum(((o - n) ** 2).sum() if hasattr((o - n), 'sum') else (o - n) ** 2 for o, n in zip(original_grad, new_grad))
            sensitivities.append(sensitivity)
            print(f'Layer {i+1}, sensitivity {sensitivity}')
        return sensitivities
                                        
    def extract_flops(self, ):
        return [module.flops for module in self.model.modules() if isinstance(module, DropBP)]

    def extract_count(self, ):
        return [module.count for module in self.model.modules() if isinstance(module, DropBP)]

    def detact_non_grad(self,):
        detach_list = []
        for module in self.model.modules():
            if isinstance(module, DropBP):
                detach_list.append(module.is_detach)
        return all(detach_list)


def setup_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)   

