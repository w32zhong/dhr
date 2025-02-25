import argparse
import os
import glob
import numpy as np
import math
from tqdm import tqdm
from multiprocessing import Pool, Manager
import pickle5 as pickle
import torch
import torch.nn as nn
import time
import faiss

def faiss_search(query_embs, corpus_embs, batch=1, topk=1000):
    print('start faiss index')
    query_embs = np.concatenate([query_embs,query_embs], axis=1)
    corpus_embs = np.concatenate([corpus_embs,corpus_embs], axis=1)

    dimension = query_embs.shape[1]
    res = faiss.StandardGpuResources()
    res.noTempMemory()
    # res.setTempMemory(1000 * 1024 * 1024) # 1G GPU memory for serving query
    flat_config = faiss.GpuIndexFlatConfig()
    flat_config.device = 0
    flat_config.useFloat16=True
    index = faiss.GpuIndexFlatIP(res, dimension, flat_config)

    print("Load index to GPU...")
    index.add(corpus_embs)

    Distance = []
    Index = []
    print("Search with batch size %d"%(batch))
    widgets = ['Progress: ',Percentage(), ' ', Bar('#'),' ', Timer(),
               ' ', ETA(), ' ', FileTransferSpeed()]
    pbar = ProgressBar(widgets=widgets, maxval=query_embs.shape[0]//batch).start()
    start_time = time.time()

    for i in range(query_embs.shape[0]//batch):
        D,I=index.search(query_embs[i*batch:(i+1)*batch], topk)


        Distance.append(D)
        Index.append(I)
        pbar.update(i + 1)


    D,I=index.search(query_embs[(i+1)*batch:], topk)


    Distance.append(D)
    Index.append(I)

    time_per_query = (time.time() - start_time)/query_embs.shape[0]
    print('Retrieving {} queries ({:0.3f} s/query)'.format(query_embs.shape[0], time_per_query))
    Distance = np.concatenate(Distance, axis=0)
    Index = np.concatenate(Index, axis=0)
    return Distance, Index

def IP_retrieval(qids, query_embs, corpus_embs, args):

    description = 'Brute force IP search'


    all_results = {}
    all_scores = {}

    start_time = time.time()
    total_num_idx = 0
    for i, (query_emb) in tqdm(enumerate(query_embs), total=len(query_embs), desc=description):

        
                
        scores = torch.einsum('ij,j->i',(corpus_embs, query_emb))
        sort_candidates = torch.argsort(scores, descending=True)[:args.topk]
        sort_scores = scores[sort_candidates]

        all_scores[qids[i]]=sort_scores.cpu().tolist()
        all_results[qids[i]]=sort_candidates.cpu().tolist()

    average_num_idx = total_num_idx/query_embs.shape[0]
    time_per_query = (time.time() - start_time)/query_embs.shape[0]
    print('Retrieving {} queries ({:0.3f} s/query), average number of index use {}'.format(query_embs.shape[0], time_per_query, average_num_idx))

    return all_results, all_scores 


def GIP_retrieval(qids, query_embs, query_arg_idxs, corpus_embs, corpus_arg_idxs, args):
    if args.brute_force:
        args.theta = 0
        description = 'Brute force GIP search'
    else:
        if not args.IP:
            if args.rerank:
                description = 'GIP (\u03F4={}) retrieval w/ GIP rerank'.format(args.theta)
            else:
                description = 'GIP (\u03F4={}) retrieval w/o GIP rerank'.format(args.theta)
        else:
            if args.rerank:
                description = 'IP retrieval w/ GIP rerank'
            else:
                description = 'IP retrieval w/o GIP rerank'

    all_results = {}
    all_scores = {}

    start_time = time.time()
    total_num_idx = 0
    for i, (query_emb, query_arg_idx) in tqdm(enumerate(zip(query_embs, query_arg_idxs)), total=len(query_embs), desc=description):

        if args.theta==0:
            total_num_idx += args.emb_dim
            candidate_sparse_embs = ((corpus_arg_idxs[:,:]==query_arg_idx)*corpus_embs[:,:args.emb_dim])                    

            if args.combine_cls:
                candidate_dense_embs = corpus_embs[:,args.emb_dim:]
                scores = torch.einsum('ij,j->i',(candidate_sparse_embs, query_emb[:args.emb_dim])) + torch.einsum('ij,j->i',(candidate_dense_embs, query_emb[args.emb_dim:]))
                del candidate_sparse_embs, candidate_dense_embs
            else:
                scores = torch.einsum('ij,j->i',(candidate_sparse_embs, query_emb[:args.emb_dim]))
                del candidate_sparse_embs
            sort_idx = torch.argsort(scores, descending=True)[:args.topk]
            sort_candidates = sort_idx
            sort_scores = scores[sort_idx]

            torch.cuda.empty_cache()

        else:
            num_idx = int((query_emb[:args.emb_dim] > args.theta).sum())

            if args.combine_cls:
                num_cls_idx = int((query_emb[args.emb_dim:] > args.theta).sum())
                important_cls_idx = torch.argsort(query_emb[args.emb_dim:], axis=0, descending=True).tolist()[:num_cls_idx]
            if args.combine_cls:
                total_num_idx += num_idx + num_cls_idx
            else:
                total_num_idx += num_idx 
            if num_idx >40:
                num_idx=40
            if num_idx==0:
                num_idx=1
            important_idx = torch.argsort(query_emb[:args.emb_dim], axis=0, descending=True).tolist()[:num_idx]

            if not args.IP:
                # Approximate GIP
                candidate_sparse_embs = ((corpus_arg_idxs[:,important_idx]==query_arg_idx[important_idx])*corpus_embs[:,important_idx])
                if args.combine_cls:

                    candidate_dense_embs = corpus_embs[:,args.emb_dim:]
                    partial_scores = torch.einsum('ij,j->i',(candidate_sparse_embs, query_emb[important_idx])) + args.lamda*torch.einsum('ij,j->i',(candidate_dense_embs[:,important_cls_idx], query_emb[args.emb_dim:][important_cls_idx]))
                    # partial_scores = torch.einsum('ij,j->i',(candidate_sparse_embs, query_emb[important_idx])) + torch.einsum('ij,j->i',(candidate_dense_embs, query_emb[args.emb_dim:]))
                else:
                    partial_scores = torch.einsum('ij,j->i',(candidate_sparse_embs, query_emb[important_idx])) 
            else:
                # IN as an approximation ablation
                if args.combine_cls:
                    candidate_sparse_embs = corpus_embs[:,:args.emb_dim]
                    candidate_dense_embs = corpus_embs[:,args.emb_dim:]

                    partial_scores = torch.einsum('ij,j->i',(candidate_sparse_embs, query_emb[:args.emb_dim])) + 1*torch.einsum('ij,j->i',(candidate_dense_embs, query_emb[args.emb_dim:]))
                else:
                    partial_scores = torch.einsum('ij,j->i',(corpus_embs, query_emb))

            if args.rerank:
                candidates = torch.argsort(partial_scores, descending=True)[:args.agip_topk]

                candidate_sparse_embs = ((corpus_arg_idxs[candidates,:]==query_arg_idx)*corpus_embs[candidates,:args.emb_dim])
                # candidate_sparse_embs = torch.where((corpus_arg_idxs[candidates,:]==query_arg_idx),corpus_embs[candidates,:args.emb_dim],torch.zeros_like(corpus_embs[candidates,:args.emb_dim]))
                if args.combine_cls:
                    candidate_dense_embs = corpus_embs[candidates,args.emb_dim:]
                    scores = torch.einsum('ij,j->i',(candidate_sparse_embs, query_emb[:args.emb_dim])) + args.lamda*torch.einsum('ij,j->i',(candidate_dense_embs, query_emb[args.emb_dim:]))
                else:
                    scores = torch.einsum('ij,j->i',(candidate_sparse_embs, query_emb[:args.emb_dim]))

                sort_idx = torch.argsort(scores, descending=True)[:args.topk]
                sort_candidates = candidates[sort_idx]
                sort_scores = scores[sort_idx]

                del important_idx, candidates, candidate_sparse_embs, scores, sort_idx
                torch.cuda.empty_cache()
            else:
                sort_candidates = torch.argsort(partial_scores, descending=True)[:args.topk]
                sort_scores = partial_scores[sort_candidates]

        all_scores[qids[i]]=sort_scores.cpu().tolist()
        all_results[qids[i]]=sort_candidates.cpu().tolist()

    average_num_idx = total_num_idx/query_embs.shape[0]
    time_per_query = (time.time() - start_time)/query_embs.shape[0]
    print('Retrieving {} queries ({:0.3f} s/query), average number of index use {}'.format(query_embs.shape[0], time_per_query, average_num_idx))

    return all_results, all_scores 

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--query_emb_path", type=str, required=True)
    parser.add_argument("--emb_dim", type=int, default=768)
    parser.add_argument("--theta", type=float, default=0.1)
    parser.add_argument("--topk", type=int, default=1000)
    parser.add_argument("--agip_topk", type=int, default=10000)
    parser.add_argument("--combine_cls", action='store_true')
    parser.add_argument("--IP", action='store_true')
    parser.add_argument("--brute_force", action='store_true')
    parser.add_argument("--index_path", type=str, required=True)
    parser.add_argument("--faiss_index_path", type=str)
    parser.add_argument("--use_gpu", action='store_true')
    parser.add_argument("--rerank", action='store_true')
    parser.add_argument("--lamda", type=float, default=1)
    parser.add_argument("--total_shrad", type=int, default=1)
    parser.add_argument("--shrad", type=int, default=0)
    parser.add_argument("--run_name", type=str, default='h2oloo')
    args = parser.parse_args()

    if not args.use_gpu:
        import mkl
        mkl.set_num_threads(1)
    else:
        torch.cuda.set_device(0)


    
    # load query embeddings
    print('Load query embeddings ...')
    with open(args.query_emb_path, 'rb') as f:
        query_embs, query_arg_idxs, qids=pickle.load(f)

    if args.use_gpu:
        query_embs = torch.from_numpy(query_embs).cuda(0)
        try:
            query_arg_idxs = torch.from_numpy(query_arg_idxs).cuda(0)
        except:
            query_arg_idxs = None
    else:
        query_embs = torch.from_numpy(query_embs.astype(np.float32))
        try:
            query_arg_idxs = torch.from_numpy(query_arg_idxs)
        except:
            query_arg_idxs = None


    
    # load index
    print('Load index ...')
    with open(args.index_path, 'rb') as f:
        corpus_embs, corpus_arg_idxs, docids=pickle.load(f)

        doc_num_per_shrad = len(docids)//args.total_shrad
        if args.shrad==(args.total_shrad-1):
            corpus_embs = corpus_embs[doc_num_per_shrad*args.shrad:]
            try:
                corpus_arg_idxs = corpus_arg_idxs[doc_num_per_shrad*args.shrad:]
            except:
                corpus_arg_idxs = None
            docids = docids[doc_num_per_shrad*args.shrad:]
        else:
            corpus_embs = corpus_embs[doc_num_per_shrad*args.shrad:doc_num_per_shrad*(args.shrad+1)]
            try:
                corpus_arg_idxs = corpus_arg_idxs[doc_num_per_shrad*args.shrad:doc_num_per_shrad*(args.shrad+1)]
            except:
                corpus_arg_idxs = None
            docids = docids[doc_num_per_shrad*args.shrad:doc_num_per_shrad*(args.shrad+1)]

        if args.use_gpu:
            corpus_embs = torch.from_numpy(corpus_embs).cuda(0)
            if corpus_arg_idxs is not None:
                corpus_arg_idxs = torch.from_numpy(corpus_arg_idxs).cuda(0)
        else:
            corpus_embs = torch.from_numpy(corpus_embs.astype(np.float32)) 
            if corpus_arg_idxs is not None:
                corpus_arg_idxs = torch.from_numpy(corpus_arg_idxs)
        # density = corpus_embs!=0
        # density = density.sum(axis=1)
        # print(torch.sum(density)/8841823/args.emb_dim)



    if query_arg_idxs is not None:
        results, scores = GIP_retrieval(qids, query_embs, query_arg_idxs, corpus_embs, corpus_arg_idxs ,args)
    else:
        results, scores = IP_retrieval(qids, query_embs, corpus_embs, args)

    if args.total_shrad==1:
        fout = open('result.trec', 'w')
    else:
        fout = open('result{}.trec'.format(args.shrad), 'w')
    for i, query_id in tqdm(enumerate(results), total=len(results), desc=f"write results"):
        result = results[query_id]
        score = scores[query_id]
        for rank, docidx in enumerate(result):
            docid = docids[docidx]
            if (docid!=query_id):
                fout.write('{} Q0 {} {} {} {}\n'.format(query_id, docid, rank+1, score[rank], args.run_name))
    fout.close()

    print('finish')


if __name__ == "__main__":
	main()