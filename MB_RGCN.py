import numpy as np
import scipy.sparse as sp
from Params import args
import Utils.TimeLogger as logger
from Utils.TimeLogger import log
import Utils.NNLayers as NNs
from Utils.NNLayers import FC, Regularize
from DataHandler import LoadData, negSamp, transpose, transToLsts
import tensorflow as tf
from tensorflow.core.protobuf import config_pb2
import pickle
import os

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'


class Recommender:
    def __init__(self, sess, data):
        self.sess = sess
        self.trnMats, self.tstInt, self.label, self.tstUsrs, args.intTypes = data

        args.user, args.item = self.trnMats[0].shape
        print('USER', args.user, 'ITEM', args.item)
        self.metrics = dict()
        mets = ['Loss', 'preLoss', 'HR', 'NDCG']
        for met in mets:
            self.metrics['Train' + met] = list()
            self.metrics['Test' + met] = list()

    def makePrint(self, name, ep, reses, save):
        ret = 'Epoch %d/%d, %s: ' % (ep, args.epoch, name)
        for metric in reses:
            val = reses[metric]
            ret += '%s = %.4f, ' % (metric, val)
            tem = name + metric
            if save and tem in self.metrics:
                self.metrics[tem].append(val)
        ret = ret[:-2] + '  '
        return ret

    def run(self):
        self.prepareModel()
        log('Model Prepared')
        if args.load_model != None:
            self.loadModel()
            stloc = len(self.metrics['TrainLoss'])
        else:
            stloc = 0
            init = tf.global_variables_initializer()
            self.sess.run(init)
            log('Variables Inited')
        for ep in range(stloc, args.epoch):
            test = (ep % 3 == 0)
            reses = self.trainEpoch()
            log(self.makePrint('Train', ep, reses, test))
            if test:
                reses = self.testEpoch()
                log(self.makePrint('Test', ep, reses, test))
            if ep % 5 == 0:
                self.saveHistory()
            print()
        reses = self.testEpoch()
        log(self.makePrint('Test', args.epoch, reses, True))
        self.saveHistory()

    def _create_norm_adj_mat(self, R):
        '''
		Create normalized adjacency matrix.
		:param R:
			user-item adjacency matrix (scipy matrix)
		:return:
			scipy.sparse.csr_matrix: Normalized adjacency matrix.
		'''
        A = sp.dok_matrix((args.user + args.item, args.user + args.item), dtype=np.float32)
        inter_M = R.tocoo().astype(np.float32)
        inter_M_t = inter_M.transpose()
        data_dict = dict(zip(zip(inter_M.row, inter_M.col + args.user), [1] * inter_M.nnz))
        data_dict.update(dict(zip(zip(inter_M_t.row + args.user, inter_M_t.col), [1] * inter_M_t.nnz)))
        A._update(data_dict)
        # norm adj matrix
        sumArr = (A > 0).sum(axis=1)
        # add epsilon to avoid divide by zero Warning
        diag = np.array(sumArr.flatten())[0] + 1e-7
        diag = np.power(diag, -0.5)
        D = sp.diags(diag)
        L = D * A * D
        # print("Already normalize adjacency matrix.")
        L = L.tocoo().astype(np.float32)
        indices = np.mat([L.row, L.col]).transpose()
        return tf.SparseTensor(indices, L.data, L.shape)

    def _create_lightGCN_embed(self, ego_embedding, behavior_ind, n_layers=2):
        all_embeddings = [ego_embedding]
        A_hat = self._create_norm_adj_mat(self.trnMats[behavior_ind])
        for k in range(n_layers):
            ego_embedding = tf.sparse.sparse_dense_matmul(A_hat, ego_embedding)
            all_embeddings.append(ego_embedding)
        all_embeddings = tf.stack(all_embeddings, 1)
        all_embeddings = tf.reduce_mean(
            input_tensor=all_embeddings, axis=1, keepdims=False
        )
        return all_embeddings

    def cascading_block(self):
        ulats = []
        ilats = []
        # ues a dictionary to store the embedding (user || item) for each behavior
        all_embeddings = {}
        # state the tf.Variables [mapping from input space to embedding space]
        UEmbed = NNs.defineParam('UEmbed', shape=[args.user, args.latdim], dtype=tf.float32,
                                 reg=True)  # [user * latDim]
        IEmbed = NNs.defineParam('IEmbed', shape=[args.item, args.latdim], dtype=tf.float32,
                                 reg=True)  # [item * latDim]
        # Equation (1): concat the user embedding and item embedding
        # [(user + item) * latDim]
        total_embeddings = tf.concat([UEmbed, IEmbed], axis=0)
        for inp in range(args.intTypes):
            layer_embeddings = total_embeddings
            # use LightGCN to embed the input
            if len(args.gcn_list) > 0:
                layer_embeddings = self._create_lightGCN_embed(layer_embeddings, inp, n_layers=args.gcn_list[inp])
            else:
                layer_embeddings = self._create_lightGCN_embed(layer_embeddings, inp)
            # Embedding Normalization (L2 Norm)
            layer_embeddings = layer_embeddings / (1e-6 + tf.sqrt(1e-6 + tf.reduce_sum(tf.square(layer_embeddings),
                                                                                       axis=-1, keepdims=True)))
            # Residual
            total_embeddings = layer_embeddings + total_embeddings
            all_embeddings[inp] = total_embeddings
            ulat, ilat = tf.split(
                all_embeddings[inp], [args.user, args.item], 0
            )
            ulats.append(ulat)
            ilats.append(ilat)
            self._get_multi_loss(ulat, ilat)  # use the latest user item embeddings to predict the interaction

    def _get_multi_loss(self, ulat, ilat):
        pckUlat = tf.nn.embedding_lookup(ulat, self.uids)
        pckIlat = tf.nn.embedding_lookup(ilat, self.iids)
        predLat = pckUlat * pckIlat * args.mult
        pred = tf.squeeze(FC(predLat, 1, reg=True, useBias=True))  # change the dimension of the pred to 1
        self.pred = pred  # update self.pred
        sampNum = tf.shape(self.iids)[0] // 2
        posPred = tf.slice(self.pred, [0], [sampNum])
        negPred = tf.slice(self.pred, [sampNum], [-1])
        self.preLoss = tf.reduce_sum(tf.maximum(0.0, 1.0 - (posPred - negPred))) / args.batch
        self.sumLoss += self.preLoss

    def prepareModel(self):
        self.uiMats = []
        self.iuMats = []
        for i in range(args.intTypes):
            idx, data, shape = transToLsts(self.trnMats[i])
            self.uiMats.append(tf.sparse.SparseTensor(idx, data, shape))
            tpmat = transpose(self.trnMats[i])
            idx, data, shape = transToLsts(tpmat)
            self.iuMats.append(tf.sparse.SparseTensor(idx, data, shape))
        self.uids = tf.placeholder(dtype=tf.int32, shape=[None])
        self.iids = tf.placeholder(dtype=tf.int32, shape=[None])

        self.sumLoss = 0
        self.cascading_block()  # update self.pred directly in the _get_multi_loss function
        self.regLoss = args.reg * Regularize()
        self.loss = self.sumLoss + self.regLoss

        globalStep = tf.Variable(0, trainable=False)
        learningRate = tf.train.exponential_decay(args.lr, globalStep, args.decay_step, args.decay, staircase=True)
        self.optimizer = tf.train.AdamOptimizer(learningRate).minimize(self.loss, global_step=globalStep)

    def sampleTrainBatch(self, batchIds):
        preSamp = list(np.random.permutation(args.item))
        temLabel = self.label[batchIds].toarray()
        batch = len(batchIds)
        temlen = batch * 2 * args.sampNum
        uIntLoc = [None] * temlen
        iIntLoc = [None] * temlen
        cur = 0
        for i in range(batch):
            posset = np.reshape(np.argwhere(temLabel[i] != 0), [-1])
            negset = negSamp(temLabel[i], preSamp)
            poslocs = np.random.choice(posset, args.sampNum)
            neglocs = np.random.choice(negset, args.sampNum)
            for j in range(args.sampNum):
                posloc = poslocs[j]
                negloc = neglocs[j]
                uIntLoc[cur] = uIntLoc[cur + temlen // 2] = batchIds[i]
                iIntLoc[cur] = posloc
                iIntLoc[cur + temlen // 2] = negloc
                cur += 1
        return uIntLoc, iIntLoc

    def trainEpoch(self):
        num = args.user
        sfIds = np.random.permutation(num)[:args.trnNum]
        epochLoss, epochPreLoss = [0] * 2
        num = len(sfIds)
        steps = int(np.ceil(num / args.batch))

        for i in range(steps):
            st = i * args.batch
            ed = min((i + 1) * args.batch, num)
            batchIds = sfIds[st: ed]

            uIntLoc, iIntLoc = self.sampleTrainBatch(batchIds)
            target = [self.optimizer, self.preLoss, self.regLoss, self.loss]
            res = self.sess.run(target, feed_dict={self.uids: uIntLoc, self.iids: iIntLoc},
                                options=config_pb2.RunOptions(report_tensor_allocations_upon_oom=True))
            preLoss, regLoss, loss = res[1:]

            epochLoss += loss
            epochPreLoss += preLoss
            log('Step %d/%d: loss = %.2f, regLoss = %.2f          ' % (i, steps, loss, regLoss), save=False,
                oneline=True)
        ret = dict()
        ret['Loss'] = epochLoss / steps
        ret['preLoss'] = epochPreLoss / steps
        return ret

    def sampleTestBatch(self, batchIds):
        batch = len(batchIds)
        temTst = self.tstInt[batchIds]
        temLabel = self.label[batchIds].toarray()
        temlen = (batch * 100)
        uIntLoc = [None] * temlen
        iIntLoc = [None] * temlen
        tstLocs = [None] * (batch)
        cur = 0
        for i in range(batch):
            posloc = temTst[i]
            negset = np.reshape(np.argwhere(temLabel[i] == 0), [-1])
            rdnNegSet = np.random.permutation(negset)[:99]
            locset = np.concatenate((rdnNegSet, np.array([posloc])))
            tstLocs[i] = locset
            for j in range(100):
                uIntLoc[cur] = batchIds[i]
                iIntLoc[cur] = locset[j]
                cur += 1
        return uIntLoc, iIntLoc, temTst, tstLocs

    def testEpoch(self):
        self.atts = [None] * args.user
        epochHit, epochNdcg = [0] * 2
        ids = self.tstUsrs
        num = len(ids)
        testbatch = np.maximum(1, args.batch * args.sampNum // 100)
        steps = int(np.ceil(num / testbatch))
        for i in range(steps):
            st = i * testbatch
            ed = min((i + 1) * testbatch, num)
            batchIds = ids[st: ed]
            uIntLoc, iIntLoc, temTst, tstLocs = self.sampleTestBatch(batchIds)
            preds = self.sess.run(self.pred, feed_dict={self.uids: uIntLoc, self.iids: iIntLoc},
                                  options=config_pb2.RunOptions(report_tensor_allocations_upon_oom=True))
            hit, ndcg = self.calcRes(np.reshape(preds, [ed - st, 100]), temTst, tstLocs)
            epochHit += hit
            epochNdcg += ndcg
            log('Step %d/%d: hit = %d, ndcg = %d          ' % (i, steps, hit, ndcg), save=False, oneline=True)
        ret = dict()
        ret['HR'] = epochHit / num
        ret['NDCG'] = epochNdcg / num
        return ret

    def calcRes(self, preds, temTst, tstLocs):
        hit = 0
        ndcg = 0
        for j in range(preds.shape[0]):
            predvals = list(zip(preds[j], tstLocs[j]))
            predvals.sort(key=lambda x: x[0], reverse=True)
            shoot = list(map(lambda x: x[1], predvals[:args.shoot]))
            if temTst[j] in shoot:
                hit += 1
                ndcg += np.reciprocal(np.log2(shoot.index(temTst[j]) + 2))
        return hit, ndcg

    def saveHistory(self):
        if args.epoch == 0:
            return
        with open('History/' + args.save_path + '.his', 'wb') as fs:
            pickle.dump(self.metrics, fs)

        saver = tf.train.Saver()
        saver.save(self.sess, 'Models/' + args.save_path)
        log('Model Saved: %s' % args.save_path)

    def loadModel(self):
        saver = tf.train.Saver()
        saver.restore(sess, 'Models/' + args.load_model)
        with open('History/' + args.load_model + '.his', 'rb') as fs:
            self.metrics = pickle.load(fs)
        log('Model Loaded')


if __name__ == '__main__':
    logger.saveDefault = True
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True

    log('Start')
    data = LoadData()
    log('Load Data')

    with tf.Session(config=config) as sess:
        recommender = Recommender(sess, data)
        recommender.run()
