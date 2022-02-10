import torch
import torch.nn.functional as F
import torch.nn as nn
import numpy as np
from algorithm.graph2route_logistics.graph2route_layers import GCNLayer, MLP
from algorithm.graph2route_logistics.step_decode import Decoder
# from algorithm.gcnru_logistics.gcn_layer import GCN_Layer
import time

class Graph2Route(nn.Module):

    def __init__(self, config):
        super(Graph2Route, self).__init__()

        self.batch_size = config['batch_size']
        self.max_nodes = config['max_num']
        self.node_dim = config.get('node_dim', 9)
        self.dynamic_feature_dim = config.get('dynamic_feature_dim', 2)
        self.voc_edges_in = config.get('voc_edges_in', 2)
        self.voc_edges_out = config.get('voc_edges_out',2)

        self.gcn_hidden_dim = config['hidden_size']
        self.gru_node_hidden_dim = config['hidden_size']
        self.gru_edge_hidden_dim = config['hidden_size']

        self.gcn_num_layers = config['gcn_num_layers']
        self.aggregation = 'mean'
        self.device = config['device']
        self.start_fea_dim = config.get('start_fea_dim', 5)
        self.num_couv = 2400
        self.cou_embed_dim = config.get('courier_embed_dim', 10)
        self.config = config
        self.cou_embed = nn.Embedding(self.num_couv, self.cou_embed_dim)

        self.gru_node_linear = nn.Linear(self.gcn_hidden_dim * self.max_nodes, self.gru_node_hidden_dim)
        self.gru_edge_linear = nn.Linear(self.gcn_hidden_dim * self.max_nodes * self.max_nodes,
                                         self.gru_edge_hidden_dim)

        self.nodes_embedding = nn.Linear(self.node_dim, self.gcn_hidden_dim, bias=False)
        self.edges_values_embedding = nn.Linear(4, self.gcn_hidden_dim, bias=False)

        self.start_embed = nn.Linear(self.start_fea_dim, self.gcn_hidden_dim + self.node_dim)#cat(node_h, node_fea, node_dynamic)

        gcn_layers = []
        for layer in range(self.gcn_num_layers):
            gcn_layers.append(GCNLayer(self.gcn_hidden_dim, self.aggregation))
        self.gcn_layers = nn.ModuleList(gcn_layers)

        self.graph_gru = nn.GRU(self.max_nodes * self.gcn_hidden_dim, self.gcn_hidden_dim, batch_first=True)
        self.graph_linear = nn.Linear(self.gcn_hidden_dim, self.max_nodes * self.gcn_hidden_dim)
        self.decoder = Decoder(
            self.gcn_hidden_dim + self.node_dim,  # self.sort_x_emb_size
            self.gcn_hidden_dim + self.node_dim,  # self.sort_emb_size
            tanh_exploration=10,  # tanh_clipping
            use_tanh = 10 > 0,
            n_glimpses = 1,
            mask_glimpses = True,
            mask_logits = True,
        )


    def forward(self, V, V_reach_mask, label_len, label, V_dispatch_mask, E_abs_dis, E_dis,
                E_pt_dif, E_dt_dif, start_fea, E_mask, V_len, cou_fea, V_decode_mask):

        B, N, H = V_reach_mask.shape[0], V_reach_mask.shape[2], self.gcn_hidden_dim  # batch size, num nodes, gcn hidden dim todo:gcn_hidden_dim-> hidden_size
        T, node_h, edge_h = V_reach_mask.shape[1], None, None
        # batch input
        batch_decoder_input = torch.zeros([B, T, self.gcn_hidden_dim + self.node_dim]).to(self.device)
        batch_init_hx = torch.randn(B * T, self.gcn_hidden_dim + self.node_dim).to(self.device)
        batch_init_cx = torch.randn(B * T, self.gcn_hidden_dim + self.node_dim).to(self.device)

        batch_V_reach_mask = V_reach_mask.reshape(B * T, N)

        batch_node_h = torch.zeros([B, T, N, self.gcn_hidden_dim]).to(self.device)
        batch_edge_h = torch.zeros([B, T, N, N, self.gcn_hidden_dim]).to(self.device)
        batch_masked_E = torch.zeros([B, T, N, N]).to(self.device)
        cou = torch.repeat_interleave(cou_fea.unsqueeze(1), repeats=T, dim = 1).reshape(B * T, -1)#(B * T, 4)
        cou_id = cou[:, 0].long()
        embed_cou = torch.cat([self.cou_embed(cou_id), cou[:, 1].unsqueeze(1)], dim=1)#(B*T, 13)

        for t in range(T):
            E_mask_t = torch.FloatTensor(E_mask[:, t, :, :]).to(V.device)#(B, T, N, N)
            graph_node = V[:, t, :, :] # V_mask: (B, T, N)

            graph_edge_abs_dis = E_abs_dis * E_mask_t  # (B, N, N)  * (B, N, N)
            graph_edge_dis = E_dis * E_mask_t
            graph_edge_pt = E_pt_dif * E_mask_t
            graph_edge_dt = E_dt_dif * E_mask_t
            batch_masked_E[:, t, :, :] = graph_edge_abs_dis

            graph_node_t = self.nodes_embedding(graph_node)  # B * N * H
            graph_edge_t = self.edges_values_embedding(
                torch.cat([graph_edge_abs_dis.unsqueeze(3), graph_edge_dis.unsqueeze(3),
                           graph_edge_pt.unsqueeze(3), graph_edge_dt.unsqueeze(3)], dim=3))  # B * N * N * H

            batch_node_h[:, t, :] = graph_node_t
            batch_edge_h[:, t, :, :] = graph_edge_t

            decoder_input = self.start_embed(start_fea[:, t, :])

            batch_decoder_input[:, t, :] = decoder_input

        for layer in range(self.gcn_num_layers):
            batch_node_h, batch_edge_h = self.gcn_layers[layer](batch_node_h.reshape(B * T, N, H),
                                                                batch_edge_h.reshape(B * T, N, N, H))

        batch_node_h, _ = self.graph_gru(batch_node_h.reshape(B, T, -1))
        batch_node_h = self.graph_linear(batch_node_h)#（B, T, N * H)

        batch_inputs = torch.cat(
            [batch_node_h.reshape(B * T, N, self.gcn_hidden_dim), V.reshape(B * T, N, self.node_dim)], dim=2). \
            permute(1, 0, 2).contiguous().clone()

        batch_enc_h = torch.cat(
            [batch_node_h.reshape(B * T, N, self.gcn_hidden_dim), V.reshape(B * T, N, self.node_dim),], dim=2). \
            permute(1, 0, 2).contiguous().clone()
        masked_E = batch_masked_E.clone()
        masked_E[:, :, :, 0] = 0

        (pointer_log_scores, pointer_argmax, final_step_mask) = \
            self.decoder(
                batch_decoder_input.reshape(B * T, self.gcn_hidden_dim + self.node_dim),
                batch_inputs.reshape(N, T * B, self.gcn_hidden_dim + self.node_dim),
                (batch_init_hx, batch_init_cx),
                batch_enc_h.reshape(N, T * B, self.gcn_hidden_dim + self.node_dim),
                batch_V_reach_mask, batch_node_h.reshape(B * T, N, self.gcn_hidden_dim),
                V.reshape(B * T, N, self.node_dim), embed_cou, V_decode_mask.reshape(B*T, N, N),
                masked_E.reshape(B * T, N, N))

        return pointer_log_scores.exp(), pointer_argmax

    def model_file_name(self):
        t = time.time()
        file_name = '+'.join([f'{k}-{self.config[k]}' for k in ['hidden_size']])
        file_name = f'{file_name}.gcnru-logistics{t}'
        return file_name


# --Dataset
from torch.utils.data import Dataset


class Graph2RouteDataset(Dataset):
    def __init__(
            self,
            mode: str,
            params: dict,  # parameters dict
    ) -> None:
        super().__init__()
        if mode not in ["train", "val", "test"]:  # "validate"
            raise ValueError
        path_key = {'train': 'train_path', 'val': 'val_path', 'test': 'test_path'}[mode]
        path = params[path_key]
        self.data = np.load(path, allow_pickle=True).item()
        # print('in dataset')
        # print('self data order dict', len(self.data['order_dict']))

    def __len__(self):
        # return len(self.data['max_nodes_num'])

        return len(self.data['V_len'])

    def __getitem__(self, index):

        E_abs_dis = self.data['E_abs_dis'][index]
        E_dis = self.data['E_dis'][index]
        E_pt_dif = self.data['E_pt_dif'][index]
        E_dt_dif = self.data['E_dt_dif'][index]

        V = self.data['V'][index]
        V_reach_mask = self.data['V_reach_mask'][index]
        V_dispatch_mask = self.data['V_dispatch_mask'][index]

        E_mask = self.data['E_mask'][index]
        label = self.data['label'][index]
        label_len = self.data['label_len'][index]
        V_len = self.data['V_len'][index]
        start_fea = self.data['start_fea'][index]
        start_idx = self.data['start_idx'][index]
        cou_fea = self.data['cou_fea'][index]
        past_x = self.data['past_x'][index]
        V_decode_mask = self.data['V_decode_mask'][index]


        return  E_abs_dis, E_dis, E_pt_dif, E_dt_dif, V, V_reach_mask, V_dispatch_mask, \
                E_mask, label, label_len, V_len, start_fea, start_idx, cou_fea, V_decode_mask



# ---Log--
from my_utils.utils import save2file_meta
def save2file(params):
    from my_utils.utils import ws
    file_name = ws + f'/output/output_1_29/{params["model"]}.csv'
    # 写表头
    head = [
        # data setting
        'dataset', 'min_num', 'max_num', 'eval_min', 'eval_max',
        # mdoel parameters
        'model', 'hidden_size',
        # training set
        'num_epoch', 'batch_size', 'lr', 'wd', 'early_stop', 'is_test', 'log_time',
        # metric result
        'lsd', 'lmd', 'krc', 'hr@1', 'hr@2', 'hr@3', 'hr@4', 'hr@5', 'hr@6', 'hr@7', 'hr@8', 'hr@9', 'hr@10',
        'ed', 'acc@1', 'acc@2', 'acc@3', 'acc@4', 'acc@5', 'acc@6', 'acc@7', 'acc@8', 'acc@9', 'acc@10',

    ]
    save2file_meta(params,file_name,head)