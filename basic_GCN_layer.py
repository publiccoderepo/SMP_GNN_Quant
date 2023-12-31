import inspect
from collections import OrderedDict
import os
from torch.nn import Identity
from sklearn import linear_model
from torch_geometric.utils import degree
from scipy.stats import kurtosis
from scipy.stats import skew
import torch
from torch.nn import Parameter, Module, ModuleDict
import torch.nn.functional as F
from torch_geometric.utils import (
    softmax,
    add_self_loops,
    remove_self_loops,
    add_remaining_self_loops,
)
import torch_scatter
from torch_scatter import scatter_add
from torch_geometric.nn.inits import glorot, zeros
from scipy.stats import kurtosis
from scipy.stats import skew


def scatter_(name, src, index, dim=0, dim_size=None):
    """Taken from an earlier version of PyG"""
    assert name in ["add", "mean", "min", "max"]

    op = getattr(torch_scatter, "scatter_{}".format(name))

    out = op(src, index, dim, None, dim_size)
    out = out[0] if isinstance(out, tuple) else out

    if name == "max":
        out[out < -10000] = 0
    elif name == "min":
        out[out > 10000] = 0

    return out


msg_special_args = set(
    [
        "edge_index",
        "edge_index_i",
        "edge_index_j",
        "size",
        "size_i",
        "size_j",
    ]
)

aggr_special_args = set(
    [
        "index",
        "dim_size",
    ]
)

update_special_args = set([])

# due to a collision with pytorch using the key "update"
REQUIRED_QUANTIZER_KEYS = ["aggregate", "message", "update_q"]



class MessagePassingQuant(Module):
    """Modified from the PyTorch Geometric message passing class"""

    def __init__(
        self, aggr="add", flow="source_to_target", node_dim=0, messagegroup_quantizers=None, initial_graph_gamma=1.0, quant_mode=None
    ):
        super(MessagePassingQuant, self).__init__()

        self.aggr = aggr
        assert self.aggr in ["add", "mean", "max"]

        self.flow = flow
        assert self.flow in ["source_to_target", "target_to_source"]

        self.node_dim = node_dim
        assert self.node_dim >= 0

        self.__msg_params__ = inspect.signature(self.message).parameters
        self.__msg_params__ = OrderedDict(self.__msg_params__)

        self.__aggr_params__ = inspect.signature(self.aggregate).parameters
        self.__aggr_params__ = OrderedDict(self.__aggr_params__)
        self.__aggr_params__.popitem(last=False)

        self.__update_params__ = inspect.signature(self.update).parameters
        self.__update_params__ = OrderedDict(self.__update_params__)
        self.__update_params__.popitem(last=False)

        msg_args = set(self.__msg_params__.keys()) - msg_special_args
        aggr_args = set(self.__aggr_params__.keys()) - aggr_special_args
        update_args = set(self.__update_params__.keys()) - update_special_args

        self.__args__ = set().union(msg_args, aggr_args, update_args)

        assert messagegroup_quantizers is not None
        self.messagegroup_quantizers = messagegroup_quantizers
        
        self.value_alpha = 1.0

        alpha_message =  torch.tensor([self.value_alpha], requires_grad=True)
        self.alpha_message = torch.nn.Parameter(alpha_message)
        alpha_aggregate =  torch.tensor([self.value_alpha], requires_grad=True)
        self.alpha_aggregate = torch.nn.Parameter(alpha_aggregate)
        alpha_update =  torch.tensor([self.value_alpha], requires_grad=True)
        self.alpha_update = torch.nn.Parameter(alpha_update)
        
    def __set_size__(self, size, index, tensor):
        if not torch.is_tensor(tensor):
            pass
        elif size[index] is None:
            size[index] = tensor.size(self.node_dim)
        elif size[index] != tensor.size(self.node_dim):
            raise ValueError(
                (
                    f"Encountered node tensor with size "
                    f"{tensor.size(self.node_dim)} in dimension {self.node_dim}, "
                    f"but expected size {size[index]}."
                )
            )

    def __collect__(self, edge_index, size, kwargs):
        i, j = (0, 1) if self.flow == "target_to_source" else (1, 0)
        ij = {"_i": i, "_j": j}

        out = {}
        for arg in self.__args__:
            if arg[-2:] not in ij.keys():
                out[arg] = kwargs.get(arg, inspect.Parameter.empty)
            else:
                idx = ij[arg[-2:]]
                data = kwargs.get(arg[:-2], inspect.Parameter.empty)

                if data is inspect.Parameter.empty:
                    out[arg] = data
                    continue

                if isinstance(data, tuple) or isinstance(data, list):
                    assert len(data) == 2
                    self.__set_size__(size, 1 - idx, data[1 - idx])
                    data = data[idx]

                if not torch.is_tensor(data):
                    out[arg] = data
                    continue

                self.__set_size__(size, idx, data)
                self.inputData = data

    
                out[arg] = data.index_select(self.node_dim, edge_index[idx])

        size[0] = size[1] if size[0] is None else size[0]
        size[1] = size[0] if size[1] is None else size[1]

        # Add special message arguments.
        out["edge_index"] = edge_index
        out["edge_index_i"] = edge_index[i]
        out["edge_index_j"] = edge_index[j]
        out["size"] = size
        out["size_i"] = size[i]
        out["size_j"] = size[j]

        # Add special aggregate arguments.
        out["index"] = out["edge_index_i"]
        out["dim_size"] = out["size_i"]

        return out

    def __distribute__(self, params, kwargs):
        out = {}
        for key, param in params.items():
            data = kwargs[key]
            if data is inspect.Parameter.empty:
                if param.default is inspect.Parameter.empty:
                    raise TypeError(f"Required parameter {key} is empty.")
                data = param.default
            out[key] = data
        return out


    def propagate(self, edge_index, size=None, edge_norm = None, k = None, name= None, **kwargs):
        
        size = [None, None] if size is None else size
        size = [size, size] if isinstance(size, int) else size
        size = size.tolist() if torch.is_tensor(size) else size
        size = list(size) if isinstance(size, tuple) else size
        assert isinstance(size, list)
        assert len(size) == 2
        kwargs = self.__collect__(edge_index, size, kwargs)

        msg_kwargs = self.__distribute__(self.__msg_params__, kwargs)
        x_j_std = kwargs['x_j'].data.std()
        
        temp_kur_skew_list = torch.zeros([6])    
      
        self.message_val = self.message(**msg_kwargs)
        
        out = self.messagegroup_quantizers[name][k]["message"](self.message_val,custom_alpha = self.alpha_message, training=self.training)
        
        kurt_val =kurtosis(out[0].data.view(-1), fisher=False)
        skew_val = (skew(out[0].data.view(-1))) 
        temp_kur_skew_list[0] = kurt_val
        temp_kur_skew_list[1] = skew_val
        
        if type(out) == tuple:
            out = out[0]
        self.message_q = out.clone()

        aggr_kwargs = self.__distribute__(self.__aggr_params__, kwargs)
        aggre_std = out.data.std()
           

        self.aggregate_val = self.aggregate(out, **aggr_kwargs)
        
        out = self.messagegroup_quantizers[name][k]["aggregate"](self.aggregate_val, custom_alpha = self.alpha_aggregate, training=self.training)
        
        kurt_val =kurtosis(out[0].data.view(-1), fisher=False)
        skew_val = (skew(out[0].data.view(-1))) 
        temp_kur_skew_list[2] = kurt_val
        temp_kur_skew_list[3] = skew_val
        
        if type(out) == tuple:
            out = out[0]
          
        self.aggregate_q = out.clone()

        update_std = out.data.std()
       
        update_kwargs = self.__distribute__(self.__update_params__, kwargs)
        self.update_val = self.update(out, **update_kwargs)
         
        out = self.messagegroup_quantizers[name][k]["update_q"](self.update_val,custom_alpha = self.alpha_update, training=self.training)

        
        kurt_val =kurtosis(out[0].data.view(-1), fisher=False)
        skew_val = (skew(out[0].data.view(-1))) 
        temp_kur_skew_list[4] = kurt_val
        temp_kur_skew_list[5] = skew_val    
    
        scale = out[1]
        zero_point = out[2]
        if type(out) == tuple:
            out = out[0]
               
        self.update_q = out.clone()
       
        return out, scale,temp_kur_skew_list

    def message(self, x_j, edge_weight): 
        return edge_weight.view(-1, 1) * x_j
  
    def aggregate(self, inputs, index, dim_size):  # pragma: no cover
        return scatter_(self.aggr, inputs, index, self.node_dim, dim_size)

    def update(self, inputs):  # pragma: no cover
        return inputs


REQUIRED_GCN_KEYS = [
    "weights",
    "inputs",
    "features",
    "norm",
]
 