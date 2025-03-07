import torch
import torch.nn as nn
import torch.nn.functional as F

import math
import numpy as np
from lib.lorentz.manifold import CustomLorentz
from lib.lorentz_equivariant.layers.LFC import GroupLorentzFullyConnected
from groupy.gconv.make_gconv_indices import *

make_indices_functions = {(1, 4): make_c4_z2_indices,
                          (4, 4): make_c4_p4_indices,
                          (1, 8): make_d4_z2_indices,
                          (8, 8): make_d4_p4m_indices}

class GroupLorentzConv2d(nn.Module):

    def __init__(
            self,
            input_stabilizer_size, 
            output_stabilizer_size,
            manifold: CustomLorentz,
            in_channels,
            out_channels,
            kernel_size,
            stride=1,
            padding=0,
            dilation=1,
            bias=True,
            LFC_normalize=False
    ):
        super(GroupLorentzConv2d, self).__init__()
        self.input_stabilizer_size=input_stabilizer_size
        self.output_stabilizer_size=output_stabilizer_size
        self.manifold = manifold
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.padding = padding
        self.bias = bias

        if isinstance(stride, int):
            self.stride = (stride, stride)
        else:
            self.stride = stride

        if isinstance(kernel_size, int):
            self.kernel_size = (kernel_size, kernel_size)
        else:
            self.kernel_size = kernel_size

        if isinstance(padding, int):
            self.padding = (padding, padding)
        else:
            self.padding = padding

        if isinstance(dilation, int):
            self.dilation = (dilation, dilation)
        else:
            self.dilation = dilation

        # here need to reconsider for group!!!
        # for nn.linear, the kernel is also flatten ad contained in the input as linear, not as sliding windows
        self.kernel_len = self.kernel_size[0] * self.kernel_size[1]
        lin_features = ((self.in_channels - 1) * self.kernel_size[0] * self.kernel_size[1]) + 1

        # Instead of using a standard linear layer, this uses LorentzFullyConnected to preserve hyperbolic properties.
        self.linearized_kernel = GroupLorentzFullyConnected(
            manifold,
            lin_features, 
            self.out_channels, 
            bias=bias,
            normalize=LFC_normalize
        )


        # Extracts sliding windows (patches) from the input tensor, similar to what a convolution does.
        # !!! here for each group, there should be diff patches, so apply unfold for each to get diff patches
        self.unfold = torch.nn.Unfold(kernel_size=(self.kernel_size[0], self.kernel_size[1]), dilation=dilation, padding=padding, stride=stride)

        self.reset_parameters()

        self.inds = self.make_transformation_indices()
    def make_transformation_indices(self):
        # to understand later!
        return make_indices_functions[(self.input_stabilizer_size, self.output_stabilizer_size)](self.kernel_size[1])

    # where and when to update? do i need to keep the weight for each as well? 
    def reset_parameters(self):
        stdv = math.sqrt(2.0 / ((self.in_channels-1) * self.kernel_size[0] * self.kernel_size[1]))
        
        self.linearized_kernel.weight.weight.data.uniform_(-stdv, stdv)
        if self.bias:
            self.linearized_kernel.weight.bias.data.uniform_(-stdv, stdv)

    def forward(self, x):
        """ x has to be in channel-last representation -> Shape = bs x H x W x C """
        ## step 1: Computes the output height and width after convolution without account for any lorenz point
        # x = (batch_size, height, width, channels)
        # !!! remember here for the channels, it is normal_channels + 1 (time)
        bsz = x.shape[0]
        h, w = x.shape[1:3]

        h_out = math.floor(
            (h + 2 * self.padding[0] - self.dilation[0] * (self.kernel_size[0] - 1) - 1) / self.stride[0] + 1)
        w_out = math.floor(
            (w + 2 * self.padding[1] - self.dilation[1] * (self.kernel_size[1] - 1) - 1) / self.stride[1] + 1)

     
        ## step 2: Extracting Patches
        # extracting local patches from the input tensor and reshaping them to prepare for further processing, like applying a linear layer or convolutional kernel.
        x = x.permute(0, 3, 1, 2)
        # x = (batch_size, channels, height, width)
        # torch.Size([128, 2, 32, 32])

        # used to extract sliding local blocks (or patches) from the input tensor
        patches = self.unfold(x)  
   
        # patches = (batch_size, channels(+1 time) * kernel_height * kernel_width, num_patches)
        # The number of elements per patch /each channels with each kernerl element: 2 * 3* 3
        # num of patches that can be extracted from each image = ((height + 2 * padding - dilation * (kernel_size - 1) - 1) / stride) + 1
        # patches = torch.Size([128, 18, 256])
        # therefore, These patches contain both spatial and time information.

        patches = patches.permute(0, 2, 1)
        
        patches_pre_kernel = self.extract_lorentz_patches(patches)

        out = self.linearized_kernel(patches_pre_kernel)  # Apply the linear layer to each patch
    
        out = out.view(bsz, self.output_stabilizer_size, h_out, w_out,self.out_channels )
        print("final", out.shape)
   
        return out

    def extract_lorentz_patches(self, patches):
                ## step 3： extract the time component from patches and treat them separately
        # Extracts the time coordinate and ensures it does not go below the hyperbolic manifold’s threshold.
        patches_time = torch.clamp(patches.narrow(-1, 0, self.kernel_len), min=self.manifold.k.sqrt())  
        # patches_time = (batch_size,  num of patches, kernel size (since it is last element and it is belong to time))
        patches_time_rescaled = torch.sqrt(torch.sum(patches_time ** 2, dim=-1, keepdim=True) - ((self.kernel_len - 1) * self.manifold.k))
       #  torch.Size([128, 256, 1])

        ## step 4: Extracts the remaining spatial components from patches.
        patches_space = patches.narrow(-1, self.kernel_len, patches.shape[-1] - self.kernel_len)
        # torch.Size([128, 256, 9])
        patches_space = patches_space.reshape(patches_space.shape[0], patches_space.shape[1], self.in_channels - 1, -1).transpose(-1, -2).reshape(patches_space.shape) 
        # torch.Size([128, 256, 9])

        ## step 5: Concatenates the rescaled time component and spatial components to maintain hyperbolic consistency
        patches_pre_kernel = torch.concat((patches_time_rescaled, patches_space), dim=-1)
        # patches = (batch_size, 1(time) + [channels(-1) * kernel_height * kernel_width], num_patches)

        return patches_pre_kernel
    
  



    
class LorentzP4MConvZ2(GroupLorentzConv2d):

    def __init__(self, *args, **kwargs):
        super(LorentzP4MConvZ2, self).__init__(input_stabilizer_size=1, output_stabilizer_size=8, *args, **kwargs)


class LorentzP4MConvP4M(GroupLorentzConv2d):

    def __init__(self, *args, **kwargs):
        super(LorentzP4MConvP4M, self).__init__(input_stabilizer_size=8, output_stabilizer_size=8, *args, **kwargs)