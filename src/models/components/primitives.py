# Copyright 2021 AlQuraishi Laboratory
# Copyright 2021 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from functools import partial
import importlib
import math
from typing import Optional, Callable, List, Tuple, Sequence
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from scipy.stats import truncnorm
from src.utils.checkpointing import get_checkpoint_fn
# from src.utils.chunk_utils import _chunk_slice
from src.utils.precision_utils import is_fp16_enabled
from src.utils.tensor_utils import (
    permute_final_dims,
    flatten_final_dims,
)

deepspeed_is_installed = importlib.util.find_spec("deepspeed") is not None
ds4s_is_installed = deepspeed_is_installed and importlib.util.find_spec("deepspeed.ops.deepspeed4science") is not None
if deepspeed_is_installed:
    import deepspeed

if ds4s_is_installed:
    from deepspeed.ops.deepspeed4science import DS4Sci_EvoformerAttention

DEFAULT_LMA_Q_CHUNK_SIZE = 1024
DEFAULT_LMA_KV_CHUNK_SIZE = 4096


def _prod(nums):
    out = 1
    for n in nums:
        out = out * n
    return out


def _calculate_fan(linear_weight_shape, fan="fan_in"):
    fan_out, fan_in = linear_weight_shape

    if fan == "fan_in":
        f = fan_in
    elif fan == "fan_out":
        f = fan_out
    elif fan == "fan_avg":
        f = (fan_in + fan_out) / 2
    else:
        raise ValueError("Invalid fan option")

    return f


def trunc_normal_init_(weights, scale=1.0, fan="fan_in"):
    shape = weights.shape
    f = _calculate_fan(shape, fan)
    scale = scale / max(1, f)
    a = -2
    b = 2
    std = math.sqrt(scale) / truncnorm.std(a=a, b=b, loc=0, scale=1)
    size = _prod(shape)
    samples = truncnorm.rvs(a=a, b=b, loc=0, scale=std, size=size)
    samples = np.reshape(samples, shape)
    with torch.no_grad():
        weights.copy_(torch.tensor(samples, device=weights.device))


def lecun_normal_init_(weights):
    trunc_normal_init_(weights, scale=1.0)


def he_normal_init_(weights):
    trunc_normal_init_(weights, scale=2.0)


def glorot_uniform_init_(weights):
    nn.init.xavier_uniform_(weights, gain=1)


def final_init_(weights):
    with torch.no_grad():
        weights.fill_(0.0)


def gating_init_(weights):
    with torch.no_grad():
        weights.fill_(0.0)


def normal_init_(weights):
    torch.nn.init.kaiming_normal_(weights, nonlinearity="linear")


def ipa_point_weights_init_(weights):
    with torch.no_grad():
        softplus_inverse_1 = 0.541324854612918
        weights.fill_(softplus_inverse_1)


def generate_sinusoidal_encodings(indices, c_s, max_pos=10_000):
    """
    Generates a sinusoidal encoding for a given tensor of residue indices.

    Args:
        indices (torch.Tensor): A tensor of residue indices with shape [*, n_res].
        c_s (int): The size of the channel dimension for the sinusoidal encoding.
        max_pos (int, optional): The maximum possible residue index. Default is 10000.

    Returns:
        torch.Tensor: A tensor with sinusoidal encodings of shape [*, n_res, c_s].
    TODO: delete this
    """
    # Create a position array of shape [max_pos, 1]
    position = torch.arange(max_pos, dtype=torch.float).unsqueeze(1)

    # Compute the div term
    div_term = torch.exp(torch.arange(0, c_s, 2).float() * -(math.log(10000.0) / c_s))

    # Initialize sinusoidal encoding matrix
    sinusoid_table = torch.zeros(max_pos, c_s).to(indices)

    # Apply sin to even indices in the array; 2i
    sinusoid_table[:, 0::2] = torch.sin(position * div_term)

    # Move sinusoid table to the same device as the indices
    sinusoid_table = sinusoid_table.to(indices.device)

    # Apply cos to odd indices in the array; 2i+1
    sinusoid_table[:, 1::2] = torch.cos(position * div_term)

    # Apply the encoding to each index in the input tensor
    encoded_indices = sinusoid_table[indices.long()]

    return encoded_indices


class Linear(nn.Linear):
    """
    A Linear layer with built-in nonstandard initializations. Called just
    like torch.nn.Linear.

    Implements the initializers in 1.11.4, plus some additional ones found
    in the code.
    """

    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        bias: bool = True,
        init: str = "default",
        init_fn: Optional[Callable[[torch.Tensor, torch.Tensor], None]] = None,
        precision=None
    ):
        """
        Args:
            in_dim:
                The final dimension of inputs to the layer
            out_dim:
                The final dimension of layer outputs
            bias:
                Whether to learn an additive bias. True by default
            init:
                The initializer to use. Choose from:

                "default": LeCun fan-in truncated normal initialization
                "relu": He initialization w/ truncated normal distribution
                "glorot": Fan-average Glorot uniform initialization
                "gating": Weights=0, Bias=1
                "normal": Normal initialization with std=1/sqrt(fan_in)
                "final": Weights=0, Bias=0

                Overridden by init_fn if the latter is not None.
            init_fn:
                A custom initializer taking weight and bias as inputs.
                Overrides init if not None.
        """
        super(Linear, self).__init__(in_dim, out_dim, bias=bias)

        if bias:
            with torch.no_grad():
                self.bias.fill_(0)

        with torch.no_grad():
            if init_fn is not None:
                init_fn(self.weight, self.bias)
            else:
                if init == "default":
                    lecun_normal_init_(self.weight)
                elif init == "relu":
                    he_normal_init_(self.weight)
                elif init == "glorot":
                    glorot_uniform_init_(self.weight)
                elif init == "gating":
                    gating_init_(self.weight)
                    if bias:
                        self.bias.fill_(1.0)
                elif init == "normal":
                    normal_init_(self.weight)
                elif init == "final":
                    final_init_(self.weight)
                else:
                    raise ValueError("Invalid init string.")

        self.precision = precision

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        d = input.dtype
        deepspeed_is_initialized = (
                deepspeed_is_installed and
                deepspeed.comm.comm.is_initialized()
        )
        if self.precision is not None:
            with torch.cuda.amp.autocast(enabled=False):
                bias = self.bias.to(dtype=self.precision) if self.bias is not None else None
                return nn.functional.linear(input.to(dtype=self.precision),
                                            self.weight.to(dtype=self.precision),
                                            bias).to(dtype=d)

        if d is torch.bfloat16 and not deepspeed_is_initialized:
            with torch.cuda.amp.autocast(enabled=False):
                bias = self.bias.to(dtype=d) if self.bias is not None else None
                return nn.functional.linear(input, self.weight.to(dtype=d), bias)

        return nn.functional.linear(input, self.weight, self.bias)


class LayerNorm(nn.Module):
    def __init__(self, c_in, eps=1e-5):
        super(LayerNorm, self).__init__()

        self.c_in = (c_in,)
        self.eps = eps

        self.weight = nn.Parameter(torch.ones(c_in))
        self.bias = nn.Parameter(torch.zeros(c_in))

    def forward(self, x):
        d = x.dtype
        deepspeed_is_initialized = (
                deepspeed_is_installed and
                deepspeed.comm.comm.is_initialized()
        )
        if d is torch.bfloat16 and not deepspeed_is_initialized:
            with torch.cuda.amp.autocast(enabled=False):
                out = nn.functional.layer_norm(
                    x,
                    self.c_in,
                    self.weight.to(dtype=d),
                    self.bias.to(dtype=d),
                    self.eps
                )
        else:
            out = nn.functional.layer_norm(
                x,
                self.c_in,
                self.weight,
                self.bias,
                self.eps,
            )

        return out


class AdaLN(nn.Module):
    """Adaptive Layer Normalization."""
    def __init__(self, normalized_shape):
        super(AdaLN, self).__init__()
        # Layer norms
        self.a_layer_norm = nn.LayerNorm(normalized_shape,  # equivalent to scale=False, offset=False in Haiku
                                         elementwise_affine=False,
                                         bias=False)
        self.s_layer_norm = nn.LayerNorm(normalized_shape,  # equivalent to scale=True, offset=False in Haiku
                                         elementwise_affine=True,
                                         bias=False)

        # Linear layers for gating and the skip connection
        dim = normalized_shape if isinstance(normalized_shape, int) else normalized_shape[-1]
        self.gating_linear = Linear(dim, dim, init='gating')
        self.skip_linear = Linear(dim, dim, bias=False, init='final')

    def forward(self, a, s):
        a = self.a_layer_norm(a)
        s = self.s_layer_norm(s)
        a = F.sigmoid(self.gating_linear(s)) * a + self.skip_linear(s)
        return a


@torch.jit.ignore
def softmax_no_cast(t: torch.Tensor, dim: int = -1) -> torch.Tensor:
    """
        Softmax, but without automatic casting to fp32 when the input is of
        type bfloat16
    """
    d = t.dtype
    deepspeed_is_initialized = (
            deepspeed_is_installed and
            deepspeed.comm.comm.is_initialized()

    )
    if d is torch.bfloat16 and not deepspeed_is_initialized:
        with torch.cuda.amp.autocast(enabled=False):
            s = torch.nn.functional.softmax(t, dim=dim)
    else:
        s = torch.nn.functional.softmax(t, dim=dim)

    return s


# @torch.jit.script
def _attention(query: torch.Tensor, key: torch.Tensor, value: torch.Tensor, biases: List[torch.Tensor]) -> torch.Tensor:
    # [*, H, C_hidden, K]
    key = permute_final_dims(key, (1, 0))

    # [*, H, Q, K]
    a = torch.matmul(query, key)

    for b in biases:
        a += b

    a = softmax_no_cast(a, -1)

    # [*, H, Q, C_hidden]
    a = torch.matmul(a, value)

    return a


@torch.jit.ignore
def _attention_chunked_trainable(
        query, key, value, biases, chunk_size, chunk_dim, checkpoint,
):
    if checkpoint and len(biases) > 2:
        raise ValueError(
            "Checkpointed version permits only permits two bias terms"
        )

    def _checkpointable_attention(q, k, v, b1, b2):
        bs = [b for b in [b1, b2] if b is not None]
        a = _attention(q, k, v, bs)
        return a

    o_chunks = []
    checkpoint_fn = get_checkpoint_fn()
    count = query.shape[chunk_dim]
    for start in range(0, count, chunk_size):
        end = start + chunk_size
        idx = [slice(None)] * len(query.shape)
        idx[chunk_dim] = slice(start, end)
        idx_tup = tuple(idx)
        q_chunk = query[idx_tup]
        k_chunk = key[idx_tup]
        v_chunk = value[idx_tup]

        def _slice_bias(b):
            idx[chunk_dim] = (
                slice(start, end) if b.shape[chunk_dim] != 1 else slice(None)
            )
            return b[tuple(idx)]

        if checkpoint:
            bias_1_chunk, bias_2_chunk = [
                _slice_bias(b) if b is not None else None
                for b in (biases + [None, None])[:2]
            ]

            o_chunk = checkpoint_fn(_checkpointable_attention,
                                    q_chunk, k_chunk, v_chunk, bias_1_chunk, bias_2_chunk
                                    )
        else:
            bias_chunks = [
                _slice_bias(b) for b in biases
            ]

            o_chunk = _attention(q_chunk, k_chunk, v_chunk, bias_chunks)

        o_chunk = o_chunk.transpose(-2, -3)
        o_chunks.append(o_chunk)

    o = torch.cat(o_chunks, dim=chunk_dim)
    return o


def compute_pair_attention_mask(mask, large_number=-1e6):
    # Compute boolean pair mask
    pair_mask = (mask[:, :, None] * mask[:, None, :]).unsqueeze(-1)  # (bs, n, n, 1)

    # Invert such that 0.0 indicates attention, 1.0 indicates no attention
    pair_mask_inv = torch.add(1, -pair_mask)

    # Multiply with large number such that 0.0 indicates attention, -large_number no attention
    return torch.mul(large_number, pair_mask_inv)


class AttentionPairBias(nn.Module):
    """Full self-attention with pair bias."""

    def __init__(
            self,
            dim,
            c_pair: int = 16,
            num_heads=8,
            dropout=0.0,
            device=None,
            dtype=None,
    ):
        """Initialize the AttentionPairBias module.
        Args:
            dim:
                Total dimension of the model.
            c_pair:
                The number of channels for the pair representation. Defaults to 16.
            num_heads:
                Number of parallel attention heads. Note that c_atom will be split across num_heads
                (i.e. each head will have dimension c_atom // num_heads).
            dropout:
                Dropout probability on attn_output_weights. Default: 0.0 (no dropout).
        """
        super().__init__()
        self.dim = dim
        self.c_pair = c_pair
        self.num_heads = num_heads
        self.dropout = dropout
        self.device = device
        self.dtype = dtype

        # Perform check for dimensionality
        assert dim % num_heads == 0, f"the model dimensionality ({dim}) should be divisible by the " \
                                     f"number of heads ({num_heads}) "

        # Projections
        self.ada_ln = AdaLN(dim)
        self.output_proj_linear = Linear(dim, dim, init='gating')
        self.output_proj_linear.bias = nn.Parameter(torch.ones(dim) * -2.0)  # gate values will be ~0.11

        # QKV projections and MHA
        self.q_linear = Linear(dim, dim, init='glorot')
        self.k_linear = Linear(dim, dim, init='glorot', bias=False)
        self.v_linear = Linear(dim, dim, init='glorot', bias=False)
        self.mha = nn.MultiheadAttention(dim, num_heads, dropout=dropout, batch_first=True,
                                         device=device, dtype=dtype)

        # Pair bias
        self.layer_norm_pair = nn.LayerNorm(self.c_pair)
        self.linear_pair = Linear(self.c_pair, self.num_heads, init='default', bias=False)

        # Gating
        self.gating_linear = Linear(dim, dim, init='gating', bias=False)
        self.attention_proj = Linear(dim, dim, init='default', bias=False)

    def forward(self, single_repr, single_proj, pair_repr, mask=None):
        """Full self-attention at the token-level with pair bias."""
        batch_size, n_tokens, embed_dim = single_repr.shape
        # Input projections
        a = self.ada_ln(single_repr, single_proj)  # AdaLN(a, s)  shape: (bs, n_tokens, c_atom)

        # Project query, key, value vectors
        q = self.q_linear(a)  # (bs, n_tokens, c_atom)
        k = self.k_linear(a)
        v = self.v_linear(a)

        # Pair bias
        pair_bias = self.linear_pair(self.layer_norm_pair(pair_repr))  # (bs, n_tokens, n_tokens, n_heads)
        if mask is not None:
            pair_bias += compute_pair_attention_mask(mask)  # pair attention mask
        pair_bias = pair_bias.permute(0, 3, 1, 2).reshape(batch_size * self.num_heads, n_tokens, n_tokens)

        # Multi-head attention
        attn_output, _ = self.mha(q, k, v, attn_mask=pair_bias, need_weights=False)  # (bs, n_tokens, c_atom)

        # Gating
        gated_output = F.sigmoid(self.gating_linear(attn_output)) * attn_output
        output = self.attention_proj(gated_output)  # (bs, n_atoms, c_atom)

        # Output projection (from adaLN-Zero)
        output = F.sigmoid(self.output_proj_linear(output)) * output
        return output


class Attention(nn.Module):
    """
    Standard multi-head attention using AlphaFold's default layer
    initialization. Allows multiple bias vectors.
    """

    def __init__(
            self,
            c_q: int,
            c_k: int,
            c_v: int,
            c_hidden: int,
            no_heads: int,
            gating: bool = True,
    ):
        """
        Args:
            c_q:
                Input dimension of query data
            c_k:
                Input dimension of key data
            c_v:
                Input dimension of value data
            c_hidden:
                Per-head hidden dimension
            no_heads:
                Number of attention heads
            gating:
                Whether the output should be gated using query data
        """
        super(Attention, self).__init__()

        self.c_q = c_q
        self.c_k = c_k
        self.c_v = c_v
        self.c_hidden = c_hidden
        self.no_heads = no_heads
        self.gating = gating

        # DISCREPANCY: c_hidden is not the per-head channel dimension, as
        # stated in the supplement, but the overall channel dimension.

        self.linear_q = Linear(
            self.c_q, self.c_hidden * self.no_heads, bias=False, init="glorot"
        )
        self.linear_k = Linear(
            self.c_k, self.c_hidden * self.no_heads, bias=False, init="glorot"
        )
        self.linear_v = Linear(
            self.c_v, self.c_hidden * self.no_heads, bias=False, init="glorot"
        )
        self.linear_o = Linear(
            self.c_hidden * self.no_heads, self.c_q, init="final"
        )

        self.linear_g = None
        if self.gating:
            self.linear_g = Linear(
                self.c_q, self.c_hidden * self.no_heads, init="gating"
            )

        self.sigmoid = nn.Sigmoid()

    def _prep_qkv(self,
                  q_x: torch.Tensor,
                  kv_x: torch.Tensor,
                  apply_scale: bool = True
                  ) -> Tuple[
        torch.Tensor, torch.Tensor, torch.Tensor
    ]:
        # [*, Q/K/V, H * C_hidden]
        q = self.linear_q(q_x)
        k = self.linear_k(kv_x)
        v = self.linear_v(kv_x)

        # [*, Q/K, H, C_hidden]
        q = q.view(q.shape[:-1] + (self.no_heads, -1))
        k = k.view(k.shape[:-1] + (self.no_heads, -1))
        v = v.view(v.shape[:-1] + (self.no_heads, -1))

        # [*, H, Q/K, C_hidden]
        q = q.transpose(-2, -3)
        k = k.transpose(-2, -3)
        v = v.transpose(-2, -3)

        if apply_scale:
            q /= math.sqrt(self.c_hidden)

        return q, k, v

    def _wrap_up(self,
                 o: torch.Tensor,
                 q_x: torch.Tensor
                 ) -> torch.Tensor:
        if self.linear_g is not None:
            g = self.sigmoid(self.linear_g(q_x))

            # [*, Q, H, C_hidden]
            g = g.view(g.shape[:-1] + (self.no_heads, -1))
            o = o * g

        # [*, Q, H * C_hidden]
        o = flatten_final_dims(o, 2)

        # [*, Q, C_q]
        o = self.linear_o(o)

        return o

    def forward(
            self,
            q_x: torch.Tensor,
            kv_x: torch.Tensor,
            biases: Optional[List[torch.Tensor]] = None,
            use_deepspeed_evo_attention: bool = False,
            use_lma: bool = False,
            lma_q_chunk_size: int = DEFAULT_LMA_Q_CHUNK_SIZE,
            lma_kv_chunk_size: int = DEFAULT_LMA_KV_CHUNK_SIZE,
    ) -> torch.Tensor:
        """
        Args:
            q_x:
                [*, Q, C_q] query data
            kv_x:
                [*, K, C_k] key data
            biases:
                List of biases that broadcast to [*, H, Q, K]
            use_deepspeed_evo_attention:
                Whether to use DeepSpeed memory-efficient attention kernel.
                If none of the "use_<...>" flags are True, a stock PyTorch
                implementation is used instead
            use_lma:
                Whether to use low-memory attention (Staats & Rabe 2021). It is
                advantageous during inference with extremely long sequences.
                If none of the "use_<...>" flags are True, a stock PyTorch
                implementation is used instead
            lma_q_chunk_size:
                Query chunk size (for LMA)
            lma_kv_chunk_size:
                Key/Value chunk size (for LMA)
        Returns
            [*, Q, C_q] attention update
        """
        if use_lma and (lma_q_chunk_size is None or lma_kv_chunk_size is None):
            raise ValueError(
                "If use_lma is specified, lma_q_chunk_size and "
                "lma_kv_chunk_size must be provided"
            )

        attn_options = [use_deepspeed_evo_attention, use_lma]
        if sum(attn_options) > 1:
            raise ValueError(
                "Choose at most one alternative attention algorithm"
            )

        if biases is None:
            biases = []

        # DeepSpeed attention kernel applies scaling internally
        q, k, v = self._prep_qkv(q_x, kv_x,
                                 apply_scale=not use_deepspeed_evo_attention)

        if use_deepspeed_evo_attention:
            if len(biases) > 2:
                raise ValueError(
                    "If use_deepspeed_evo_attention is True, you may only "
                    "provide up to two bias terms"
                )
            o = _deepspeed_evo_attn(q, k, v, biases)
        elif use_lma:
            biases = [
                b.expand(b.shape[:-2] + (q_x.shape[-2],) + (kv_x.shape[-2],))
                for b in biases
            ]
            o = _lma(q, k, v, biases, lma_q_chunk_size, lma_kv_chunk_size)
            o = o.transpose(-2, -3)
        else:
            o = _attention(q, k, v, biases)
            o = o.transpose(-2, -3)

        o = self._wrap_up(o, q_x)

        return o


class GlobalAttention(nn.Module):
    def __init__(self, c_in, c_hidden, no_heads, inf, eps):
        super(GlobalAttention, self).__init__()

        self.c_in = c_in
        self.c_hidden = c_hidden
        self.no_heads = no_heads
        self.inf = inf
        self.eps = eps

        self.linear_q = Linear(
            c_in, c_hidden * no_heads, bias=False, init="glorot"
        )

        self.linear_k = Linear(
            c_in, c_hidden, bias=False, init="glorot",
        )
        self.linear_v = Linear(
            c_in, c_hidden, bias=False, init="glorot",
        )
        self.linear_g = Linear(c_in, c_hidden * no_heads, init="gating")
        self.linear_o = Linear(c_hidden * no_heads, c_in, init="final")

        self.sigmoid = nn.Sigmoid()

    def forward(self,
                m: torch.Tensor,
                mask: torch.Tensor,
                use_lma: bool = False,
                ) -> torch.Tensor:
        # [*, N_res, C_in]
        q = torch.sum(m * mask.unsqueeze(-1), dim=-2) / (
                torch.sum(mask, dim=-1)[..., None] + self.eps
        )

        # [*, N_res, H * C_hidden]
        q = self.linear_q(q)
        q *= (self.c_hidden ** (-0.5))

        # [*, N_res, H, C_hidden]
        q = q.view(q.shape[:-1] + (self.no_heads, -1))

        # [*, N_res, N_seq, C_hidden]
        k = self.linear_k(m)
        v = self.linear_v(m)

        bias = (self.inf * (mask - 1))[..., :, None, :]
        if not use_lma:
            # [*, N_res, H, N_seq]
            a = torch.matmul(
                q,
                k.transpose(-1, -2),  # [*, N_res, C_hidden, N_seq]
            )
            a += bias
            a = softmax_no_cast(a)

            # [*, N_res, H, C_hidden]
            o = torch.matmul(
                a,
                v,
            )
        else:
            o = _lma(
                q,
                k,
                v,
                [bias],
                DEFAULT_LMA_Q_CHUNK_SIZE,
                DEFAULT_LMA_KV_CHUNK_SIZE
            )

        # [*, N_res, N_seq, C_hidden]
        g = self.sigmoid(self.linear_g(m))

        # [*, N_res, N_seq, H, C_hidden]
        g = g.view(g.shape[:-1] + (self.no_heads, -1))

        # [*, N_res, N_seq, H, C_hidden]
        o = o.unsqueeze(-3) * g

        # [*, N_res, N_seq, H * C_hidden]
        o = o.reshape(o.shape[:-2] + (-1,))

        # [*, N_res, N_seq, C_in]
        m = self.linear_o(o)

        return m


@torch.jit.ignore
def _deepspeed_evo_attn(
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        biases: List[torch.Tensor],
):
    """""
    Compute attention using the DeepSpeed DS4Sci_EvoformerAttention kernel.

    Args:
        q:
            [*, H, Q, C_hidden] query data
        k:
            [*, H, K, C_hidden] key data
        v:
            [*, H, V, C_hidden] value data
        biases:
            List of biases that broadcast to [*, H, Q, K]
    """

    if not ds4s_is_installed:
        raise ValueError(
            "_deepspeed_evo_attn requires that DeepSpeed be installed "
            "and that the deepspeed.ops.deepspeed4science package exists"
        )

    def reshape_dims(x):
        no_batch_dims = len(x.shape[:-3])
        if no_batch_dims < 2:
            return x.reshape(*((1,) * (2 - no_batch_dims) + x.shape))
        if no_batch_dims > 2:
            return x.reshape(*((x.shape[0], -1) + x.shape[-3:]))
        return x

    # [*, Q/K, H, C_hidden]
    q = q.transpose(-2, -3)
    k = k.transpose(-2, -3)
    v = v.transpose(-2, -3)

    # Reshape tensors to match expected input shape [B, N, Q/K, H, C_hidden]
    # for DS4Sci_EvoformerAttention() by adding or flattening batch dims as needed.
    orig_shape = q.shape
    if len(orig_shape[:-3]) != 2:
        q = reshape_dims(q)
        k = reshape_dims(k)
        v = reshape_dims(v)
        biases = [reshape_dims(b) for b in biases]

    # DeepSpeed attn. kernel requires inputs to be type bf16 or fp16
    # Cast to bf16 so kernel can be used during inference
    orig_dtype = q.dtype
    if orig_dtype not in [torch.bfloat16, torch.float16]:
        o = DS4Sci_EvoformerAttention(q.to(dtype=torch.bfloat16),
                                      k.to(dtype=torch.bfloat16),
                                      v.to(dtype=torch.bfloat16),
                                      [b.to(dtype=torch.bfloat16) for b in biases])

        o = o.to(dtype=orig_dtype)
    else:
        o = DS4Sci_EvoformerAttention(q, k, v, biases)

    o = o.reshape(orig_shape)
    return o


def _lma(
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        biases: List[torch.Tensor],
        q_chunk_size: int,
        kv_chunk_size: int,
):
    no_q, no_kv = q.shape[-2], k.shape[-2]

    # [*, H, Q, C_hidden]
    o = q.new_zeros(q.shape)
    for q_s in range(0, no_q, q_chunk_size):
        q_chunk = q[..., q_s: q_s + q_chunk_size, :]
        large_bias_chunks = [
            b[..., q_s: q_s + q_chunk_size, :] for b in biases
        ]

        maxes = []
        weights = []
        values = []
        for kv_s in range(0, no_kv, kv_chunk_size):
            k_chunk = k[..., kv_s: kv_s + kv_chunk_size, :]
            v_chunk = v[..., kv_s: kv_s + kv_chunk_size, :]
            small_bias_chunks = [
                b[..., kv_s: kv_s + kv_chunk_size] for b in large_bias_chunks
            ]

            a = torch.einsum(
                "...hqd,...hkd->...hqk", q_chunk, k_chunk,
            )

            for b in small_bias_chunks:
                a += b

            max_a = torch.max(a, dim=-1, keepdim=True)[0]
            exp_a = torch.exp(a - max_a)
            exp_v = torch.einsum("...hvf,...hqv->...hqf", v_chunk, exp_a)

            maxes.append(max_a.detach().squeeze(-1))
            weights.append(torch.sum(exp_a, dim=-1))
            values.append(exp_v)

        chunk_max = torch.stack(maxes, dim=-3)
        chunk_weights = torch.stack(weights, dim=-3)
        chunk_values = torch.stack(values, dim=-4)

        global_max = torch.max(chunk_max, dim=-3, keepdim=True)[0]
        max_diffs = torch.exp(chunk_max - global_max)
        chunk_values = chunk_values * max_diffs.unsqueeze(-1)
        chunk_weights = chunk_weights * max_diffs

        all_values = torch.sum(chunk_values, dim=-4)
        all_weights = torch.sum(chunk_weights.unsqueeze(-1), dim=-4)

        q_chunk_out = all_values / all_weights

        o[..., q_s: q_s + q_chunk_size, :] = q_chunk_out

    return o
