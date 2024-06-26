# Copyright (c) 2024 Neural Magic, All Rights Reserved.
# AQLM quantization technique, as described in the paper:
# https://arxiv.org/pdf/2401.06118.pdf

from typing import Any, Dict, List, Optional
from contextlib import suppress

import math
import torch
from torch.nn.parameter import Parameter
import torch.nn.functional as F

from aphrodite.modeling.layers.linear import LinearMethodBase, set_weight_attrs
from aphrodite.quantization.base_config import (QuantizationConfig)

HAS_QUANTS = False
with suppress(ImportError):
    from aphrodite._quant_C import quant_ops as ops
    HAS_QUANTS = True


def get_int_dtype(nbits: int) -> torch.dtype:
    if nbits <= 8:
        return torch.int8
    if nbits <= 16:
        return torch.int16
    if nbits <= 32:
        return torch.int32
    if nbits <= 64:
        return torch.int64
    raise ValueError(f"No dtype available for {nbits}-bit codebooks")


@torch.inference_mode()
def unpack_int_data(data: torch.IntTensor, nbits: int) -> torch.IntTensor:
    return data.to(torch.int64) % (2**nbits)


def dequantize_weight(codes: torch.Tensor,
                      codebooks: torch.Tensor,
                      scales: Optional[torch.Tensor] = None) -> torch.Tensor:
    """
    Decode float weights from quantization codes. Differentiable.
    :param codes: tensor of integer quantization codes, shape [*dims,
        num_out_groups, num_in_groups, num_codebooks]
    :param codebooks: tensor of vectors for each quantization code,
        [num_codebooks, codebook_size, out_group_size, in_group_size]
    :param scales: weight will be multiplied by this factor, must be
        broadcastble with [*dims, out_groups, num_in_groups, out_group_size,
        in_group_size]
    :return: reconstructed weight tensor of shape [*dims,
        num_in_groups*group_size]
    """
    num_out_groups, num_in_groups, num_codebooks = codes.shape[-3:]
    (num_codebooks, codebook_size, out_group_size,
     in_group_size) = codebooks.shape
    out_features = num_out_groups * out_group_size
    in_features = num_in_groups * in_group_size
    codebook_offsets = torch.arange(
        0, num_codebooks * codebook_size, codebook_size,
        device=codes.device)  # shape: [num_codebooks]
    reconstructed_weight_flat = F.embedding_bag(
        codes.flatten(0, -2) + codebook_offsets,
        codebooks.flatten(0, 1).flatten(-2, -1),
        mode="sum"
    )  # [prod(dims) * num_out_groups * num_in_groups, out_group_size *
    # in_group_size]

    reconstructed_weight_groupwise = reconstructed_weight_flat.view(
        list(codes.shape[:-3]) +
        [num_out_groups, num_in_groups, out_group_size, in_group_size])
    if scales is not None:
        reconstructed_weight_groupwise = reconstructed_weight_groupwise.mul(
            scales)
    return reconstructed_weight_groupwise.swapaxes(
        -3, -2).reshape(list(codes.shape[:-3]) + [out_features, in_features])


def dequantize_gemm(
    input: torch.Tensor,  #  [..., in_features]
    codes: torch.IntTensor,  #  [num_out_groups, num_in_groups, num_codebooks]
    codebooks: torch.
    Tensor,  #  [num_codebooks, codebook_size, out_group_size, in_group_size]
    scales: torch.Tensor,  #  [num_out_groups, 1, 1, 1]
    bias: Optional[torch.Tensor],
) -> torch.Tensor:
    dequantized_weight = dequantize_weight(
        unpack_int_data(codes, codebooks.shape[1].bit_length() - 1),
        codebooks,
        scales,
    )
    return F.linear(input, dequantized_weight, bias)


def dequantize_partioned_gemm(
    input: torch.Tensor,  #  [..., in_features]
    codes: torch.IntTensor,  #  [num_out_groups, num_in_groups, num_codebooks]
    codebooks: torch.
    Tensor,  #  [num_codebooks, codebook_size, out_group_size, in_group_size]
    scales: torch.Tensor,  #  [num_out_groups, 1, 1, 1]
    output_partition_sizes: torch.IntTensor,
    bias: Optional[torch.Tensor],
) -> torch.Tensor:
    output_shape = input.shape[:-1] + (scales.shape[0], )
    output = torch.empty(output_shape, dtype=input.dtype, device=input.device)
    num_outputs = len(output_partition_sizes)

    # break the inputs and codebooks apart then combine the outputs.
    # Surprisingly (to me) this is faster than doing 3 de-quants and 1 big
    # multiply at the end.
    num_codebooks = codebooks.shape[0] // num_outputs
    assert (scales.shape[0] == codes.shape[0])
    assert (sum(output_partition_sizes) == scales.shape[0])
    output_offset = 0
    codebooks_offset = 0
    for output_size in output_partition_sizes:
        shard_output = dequantize_gemm(
            input, codes.narrow(0, output_offset, output_size),
            codebooks.narrow(0, codebooks_offset, num_codebooks),
            scales.narrow(0, output_offset, output_size), None
            if bias is None else bias.narrow(0, output_offset, output_size))

        output_slice = output.narrow(-1, output_offset, output_size)
        assert (output_slice.shape == shard_output.shape)
        output_slice.copy_(shard_output)
        output_offset += output_size
        codebooks_offset += num_codebooks
    return output


class AQLMConfig(QuantizationConfig):
    """Config class for AQLM.
    Reference: https://github.com/Vahe1994/AQLM
    """

    def __init__(
        self,
        in_group_size: int,
        nbits_per_codebook: int,
        num_codebooks: int,
        out_group_size: int,
    ) -> None:
        if not HAS_QUANTS:
            raise ImportError("Could not find the quantization kernels.")
        self.in_group_size = in_group_size
        self.nbits_per_codebook = nbits_per_codebook
        self.num_codebooks = num_codebooks
        self.out_group_size = out_group_size

        # out_group_size > 1 is untested, and probably won't work as-is.
        assert self.out_group_size == 1
        self.pack_factor = (self.in_group_size * self.out_group_size)

    def __repr__(self) -> str:
        return (f"AQLMConfig(in_group_size={self.in_group_size}, "
                f"nbits_per_codebook={self.nbits_per_codebook}, "
                f"num_codebooks={self.num_codebooks}, "
                f"out_group_size={self.out_group_size})")

    @classmethod
    def get_name(cls) -> str:
        return "aqlm"

    @classmethod
    def get_supported_act_dtypes(cls) -> List[torch.dtype]:
        return [torch.half]

    @classmethod
    def get_min_capability(cls) -> int:
        return 70

    @classmethod
    def get_config_filenames(cls) -> List[str]:
        return []  # no extra configs.

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "AQLMConfig":
        in_group_size = cls.get_from_keys(config, ["in_group_size"])
        nbits_per_codebook = cls.get_from_keys(config, ["nbits_per_codebook"])
        num_code_books = cls.get_from_keys(config, ["num_codebooks"])
        out_group_size = cls.get_from_keys(config, ["out_group_size"])
        return cls(in_group_size, nbits_per_codebook, num_code_books,
                   out_group_size)

    def get_linear_method(self) -> "AQLMLinearMethod":
        return AQLMLinearMethod(self)

    def get_scaled_act_names(self) -> List[str]:
        return []

    def merge_weight(self) -> bool:
        return True

    def rope_style(self) -> Optional[bool]:
        return None

    def quant_vocab(self) -> List[bool]:
        return [False, False]

    def support_fused_moe(self) -> bool:
        return False


class AQLMLinearMethod(LinearMethodBase):
    """Linear method for AQLM.
    Args:
        quant_config: The AQLM quantization config.
    """

    def __init__(self, quant_config: AQLMConfig):
        self.quant_config = quant_config

    def create_weights(
        self,
        input_size_per_partition: int,
        output_partition_sizes: List[int],
        input_size: int,
        output_size: int,
        params_dtype: torch.dtype,
    ) -> Dict[str, Any]:
        del output_size  # Unused.
        del input_size  # Unused.

        if params_dtype != torch.half:
            raise ValueError("Only half is currently supported by aqlm")
        if input_size_per_partition % self.quant_config.in_group_size != 0:
            raise ValueError(
                "The input size is not aligned with the quantized "
                "weight shape. This can be caused by too large "
                "tensor parallel size.")

        output_size_per_partition = sum(output_partition_sizes)
        if output_size_per_partition % self.quant_config.out_group_size != 0:
            raise ValueError(
                "The output size is not aligned with the quantized "
                "weight shape. This can be caused by too large "
                "tensor parallel size.")

        codes = Parameter(
            torch.empty(
                # There could actually be two pack factors, one along input
                # and one along output,
                # but we don't currently support out_group_size,
                # and only the one along output needs to be marked with
                # "packed_dim".
                # in order for QKVLinear to work.
                output_size_per_partition,
                input_size_per_partition // self.quant_config.pack_factor,
                self.quant_config.num_codebooks,
                dtype=get_int_dtype(self.quant_config.nbits_per_codebook),
            ),
            requires_grad=False,
        )

        set_weight_attrs(
            codes,
            {
                "input_dim": 1,
                "output_dim": 0,
                "packed_dim": 1,
                "pack_factor": self.quant_config.pack_factor,
            },
        )

        codebooks = Parameter(
            torch.empty(
                self.quant_config.num_codebooks * len(output_partition_sizes),
                2**self.quant_config.nbits_per_codebook,
                self.quant_config.out_group_size,
                self.quant_config.in_group_size,
                dtype=params_dtype,
            ),
            requires_grad=False,
        )
        set_weight_attrs(
            codebooks,
            {
                # metadata indicates fixed size concatenated along dim 0
                "is_metadata":
                True,
                "output_partition_sizes":
                torch.tensor(output_partition_sizes, device='cpu'),
            },
        )

        scales = Parameter(
            torch.empty(
                (
                    output_size_per_partition //
                    self.quant_config.out_group_size,
                    1,
                    1,
                    1,
                ),
                dtype=params_dtype,
            ),
            requires_grad=False,
        )
        set_weight_attrs(
            scales,
            {
                "output_dim": 0,
                "packed_dim": 0,
                "pack_factor": self.quant_config.out_group_size
            },
        )

        return {
            "codes": codes,
            "codebooks": codebooks,
            "scales": scales,
        }

    def apply_weights(
        self,
        weights: Dict[str, Any],
        x: torch.Tensor,
        bias: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        codebooks = weights["codebooks"]
        codes = weights["codes"]
        scales = weights["scales"]
        output_partition_sizes = getattr(codebooks, "output_partition_sizes",
                                         None)

        use_gemv = math.prod(
            x.shape[:-1]) <= 32 or output_partition_sizes is None
        output = ops.aqlm_gemm(
            x,
            codes,
            codebooks,
            scales,
            output_partition_sizes,
            bias,
        ) if use_gemv else dequantize_partioned_gemm(
            x,
            codes,
            codebooks,
            scales,
            output_partition_sizes,
            bias,
        )

        return output

    def apply_moe_weights(self, w1: Dict[str,
                                         torch.Tensor], w2: Dict[str,
                                                                 torch.Tensor],
                          x: torch.Tensor, gating_output: torch.Tensor,
                          topk: int, renormalize: bool) -> torch.Tensor:
        raise NotImplementedError
