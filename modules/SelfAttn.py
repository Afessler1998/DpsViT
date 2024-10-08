# © 2024 Alec Fessler
# MIT License
# See LICENSE file in the project root for full license information.

from torch import nn
import torch.nn.init as init
from timm.models.layers import DropPath

class SelfAttn(nn.Module):
    def __init__(
        self,
        embed_dim,
        num_heads,
        stochastic_depth=0.0
    ):
        super(SelfAttn, self).__init__()
        self.num_heads = num_heads
        self.head_dim = embed_dim // num_heads
        assert embed_dim % num_heads == 0, f"embed_dim must be divisible by num_heads"

        self.norm = nn.LayerNorm(embed_dim)
        self.drop_path = DropPath(stochastic_depth) if stochastic_depth > 0.0 else nn.Identity()

        self.attn = nn.MultiheadAttention(embed_dim, num_heads)
        self.fc1 = nn.Linear(embed_dim, embed_dim * 4)
        self.activation = nn.GELU()
        self.fc2 = nn.Linear(embed_dim * 4, embed_dim)

        self.init_weights()

    def init_weights(self):
        init.xavier_uniform_(self.fc1.weight)
        init.xavier_uniform_(self.fc2.weight)

    def forward(self, x, mask=None):
        residual = x
        x = self.norm(x)
        attn_output, attn_weights = self.attn(x, x, x, key_padding_mask=mask)

        x = x + attn_output
        x = self.norm(x)

        x_ff = self.fc1(x)
        x_ff = self.activation(x_ff)
        x_ff = self.fc2(x_ff)

        return residual + self.drop_path(x_ff), attn_weights
