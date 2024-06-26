# a hack to deal with not installing as a library
import sys
sys.path.append('.')

import pytest
import torch
import torch.nn as nn
from transformers import ViTConfig
from transformers.models.vit.modeling_vit import ViTLayer
from model.transformer import (MultiheadAttention, TransformerEncoder, TransformerDecoder,
                               DETREncoderLayer, DETRDecoderLayer, DETRTransformer,
                               ViTEncoderLayer)
# TODO: Maybe just to test against huggingface implementation to make things simplier
from tests.transformer_detr import TransformerEncoderLayer as TransformerEncoderLayer_old
from tests.transformer_detr import TransformerDecoderLayer as TransformerDecoderLayer_old
from tests.transformer_detr import TransformerEncoder as TransformerEncoder_old
from tests.transformer_detr import TransformerDecoder as TransformerDecoder_old
from tests.transformer_detr import Transformer as Transformer_old


def copy_attn_weight(mha, torch_mha, dim):
  with torch.no_grad():
    mha.q_proj.weight.copy_(torch_mha.in_proj_weight[:dim])
    mha.q_proj.bias.copy_(torch_mha.in_proj_bias[:dim])
    mha.k_proj.weight.copy_(torch_mha.in_proj_weight[dim:dim*2])
    mha.k_proj.bias.copy_(torch_mha.in_proj_bias[dim:dim*2])
    mha.v_proj.weight.copy_(torch_mha.in_proj_weight[dim*2:])
    mha.v_proj.bias.copy_(torch_mha.in_proj_bias[dim*2:])
    mha.out_proj.weight.copy_(torch_mha.out_proj.weight)
    mha.out_proj.bias.copy_(torch_mha.out_proj.bias)

@pytest.fixture(scope="module")
def device():
  return torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

def test_multihead_attention(device):
  x = torch.rand(4, 32, 64).to(device)
  x_mask = (torch.rand(4, 32) < 0.3).to(device)

  torch_mha = nn.MultiheadAttention(64, 8, dropout=0.0, batch_first=True).to(device)
  mha = MultiheadAttention(64, 8, dropout=0.0).to(device)
  copy_attn_weight(mha, torch_mha, 64)

  torch_output = torch_mha(x, x, x, key_padding_mask=None, need_weights=False)[0]
  output = mha(x, x, x, key_padding_mask=None)

  assert torch.allclose(output, torch_output, rtol=1e-5, atol=1e-5), f"MultiheadAttention output miss match (without mask)!"

  torch_output = torch_mha(x, x, x, key_padding_mask=x_mask, need_weights=False)[0]
  output = mha(x, x, x, key_padding_mask=~x_mask)

  assert torch.allclose(output, torch_output, rtol=1e-5, atol=1e-5), f"MultiheadAttention output miss match (with mask)!"

def test_detr_transformer_encoder(device):
  x = torch.rand(4, 32, 64).to(device)
  x_mask = (torch.rand(4, 32) < 0.3).to(device)
  x_embed = torch.rand(4, 32, 64).to(device)

  encoder_layer_old = TransformerEncoderLayer_old(64, 8, 128, dropout=0.0).to(device)
  layer_old_output = encoder_layer_old(x.transpose(0,1), src_key_padding_mask=x_mask, pos=x_embed.transpose(0,1))

  encoder_layer = DETREncoderLayer(64, 8, 128, dropout=0.0).to(device)
  encoder_layer.load_state_dict(encoder_layer_old.state_dict(), strict=False)
  copy_attn_weight(encoder_layer.self_attn, encoder_layer_old.self_attn, 64)
  layer_output = encoder_layer(x, src_key_padding_mask=~x_mask, src_pos=x_embed)

  assert torch.allclose(layer_output, layer_old_output.transpose(0,1), rtol=1e-5, atol=1e-5), f"TransformerEncoderLayer output miss match (with mask)!"

  encoder_old = TransformerEncoder_old(encoder_layer_old, 6)
  encoder_old_output = encoder_old(x.transpose(0,1), src_key_padding_mask=x_mask, pos=x_embed.transpose(0,1))

  encoder = TransformerEncoder(encoder_layer, 6)
  encoder_output = encoder(x, ~x_mask, x_embed)

  assert torch.allclose(encoder_output, encoder_old_output.transpose(0,1), rtol=1e-5, atol=1e-5), f"TransformerEncoder output miss match (with mask)!"

def test_detr_transformer_decoder(device):
  x = torch.rand(4, 32, 64).to(device)
  x_mask = (torch.rand(4, 32) < 0.3).to(device)
  x_embed = torch.rand(4, 32, 64).to(device)
  y = torch.rand(4, 16, 64).to(device)
  y_embed = torch.rand(4, 16, 64).to(device)

  decoder_layer_old = TransformerDecoderLayer_old(64, 8, 128, dropout=0.0).to(device)
  layer_old_output = decoder_layer_old(tgt=y.transpose(0,1), memory=x.transpose(0,1), memory_key_padding_mask=x_mask,
                                       pos=x_embed.transpose(0,1), query_pos=y_embed.transpose(0,1))

  decoder_layer = DETRDecoderLayer(64, 8, 128, dropout=0.0).to(device)
  decoder_layer.load_state_dict(decoder_layer_old.state_dict(), strict=False)
  copy_attn_weight(decoder_layer.self_attn, decoder_layer_old.self_attn, 64)
  copy_attn_weight(decoder_layer.cross_attn, decoder_layer_old.multihead_attn, 64)
  layer_output = decoder_layer(memory=x, memory_key_padding_mask=~x_mask, memory_pos=x_embed, tgt=y, query_pos=y_embed)

  assert torch.allclose(layer_output, layer_old_output.transpose(0,1), rtol=1e-5, atol=1e-5), f"TransformerDecoderLayer output miss match (with mask)!"

  decoder_old = TransformerDecoder_old(decoder_layer_old, 6, nn.LayerNorm(64)).to(device)
  decoder_old_output = decoder_old(y.transpose(0,1), x.transpose(0,1), memory_key_padding_mask=x_mask, pos=x_embed.transpose(0,1),
                                   query_pos=y_embed.transpose(0,1)).squeeze(0)

  decoder = TransformerDecoder(decoder_layer, 6).to(device)
  decoder_output = decoder(x, ~x_mask, x_embed, y, y_embed)

  assert torch.allclose(decoder_output, decoder_old_output.transpose(0,1), rtol=1e-5, atol=1e-5), f"TransformerDecoder output miss match (with mask)!"

def test_vit_encoder(device):
  x = torch.rand(4, 32, 64).to(device)

  configuration = ViTConfig(hidden_size=64, num_attention_heads=8, intermediate_size=128, layer_norm_eps=1e-05, 
                            hidden_act="gelu", hidden_dropout_prob=0.0, attention_probs_dropout_prob=0.0)
  encoder_hf = ViTLayer(configuration).to(device)
  encoder = ViTEncoderLayer(d_model=64, nhead=8, dim_feedforward=128, dropout=0.0).to(device)
  with torch.no_grad():
    encoder_hf.attention.attention.query.weight.copy_(encoder.self_attn.q_proj.weight)
    encoder_hf.attention.attention.query.bias.copy_(encoder.self_attn.q_proj.bias)
    encoder_hf.attention.attention.key.weight.copy_(encoder.self_attn.k_proj.weight)
    encoder_hf.attention.attention.key.bias.copy_(encoder.self_attn.k_proj.bias)
    encoder_hf.attention.attention.value.weight.copy_(encoder.self_attn.v_proj.weight)
    encoder_hf.attention.attention.value.bias.copy_(encoder.self_attn.v_proj.bias)
    encoder_hf.attention.output.dense.weight.copy_(encoder.self_attn.out_proj.weight)
    encoder_hf.attention.output.dense.bias.copy_(encoder.self_attn.out_proj.bias)
    encoder_hf.intermediate.dense.weight.copy_(encoder.linear1.weight)
    encoder_hf.intermediate.dense.bias.copy_(encoder.linear1.bias)
    encoder_hf.output.dense.weight.copy_(encoder.linear2.weight)
    encoder_hf.output.dense.bias.copy_(encoder.linear2.bias)
  
  output_hf = encoder_hf(x)
  output = encoder(x)
  
  assert torch.allclose(output, output_hf[0], rtol=1e-5, atol=1e-5), f"ViTEncoder output miss match!"

def test_detr_transformer(device):
  x = torch.rand(4, 32, 8, 8).to(device)
  x_embed = torch.rand(4, 32, 8, 8).to(device)
  x_mask = (torch.rand(4, 8, 8) < 0.3).to(device)
  y_embed = torch.rand(64, 32).to(device)

  transformer_old = Transformer_old(32, 8, 6, 6, 64, dropout=0.0).to(device)
  transformer_old_output = transformer_old(x, x_mask, y_embed, x_embed)[0]

  transformer = DETRTransformer(32, 8, 6, 6, 64, dropout=0.0).to(device)
  transformer.load_state_dict(transformer_old.state_dict(), strict=False)
  for i in range(6):
    copy_attn_weight(transformer.encoder.layers[i].self_attn, transformer_old.encoder.layers[i].self_attn, 32)
    copy_attn_weight(transformer.decoder.layers[i].self_attn, transformer_old.decoder.layers[i].self_attn, 32)
    copy_attn_weight(transformer.decoder.layers[i].cross_attn, transformer_old.decoder.layers[i].multihead_attn, 32)
  transformer_output = transformer(x.flatten(2).transpose(1,2), ~x_mask.flatten(1), x_embed.flatten(2).transpose(1,2), y_embed.unsqueeze(0).repeat(4,1,1))

  assert torch.allclose(transformer_output, transformer_old_output.squeeze(0), rtol=1e-5, atol=1e-5), f"Transformer output miss match (with mask)!"