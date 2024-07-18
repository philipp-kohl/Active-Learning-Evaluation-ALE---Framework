import logging
from threading import Lock
from typing import List, Dict, Any,Tuple

from numpy.linalg import norm
import numpy as np
from ale.teacher.teacher_utils import tfidf_vectorize, bert_vectorize, ClusterDocument, ClusteredDocuments
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from ale.config import NLPTask
from ale.corpus.corpus import Corpus
from ale.registry.registerable_teacher import TeacherRegistry
from ale.teacher.base_teacher import BaseTeacher
from ale.trainer.predictor import Predictor

logger = logging.getLogger(__name__)
lock = Lock()

def silhouette_analysis(nr_labels: int, seed: int, embeddings) -> int:
        # use silhouette score to get best k
        ks: List[int] = np.arange(2,max(10,2*nr_labels)) # range from 2 to maximum of double the size of labels and 10
        best_k_with_score: Tuple[int,float] = [-1,-1]
        for k in ks:
            model_test = KMeans(n_clusters=k,init='k-means++',max_iter=300,n_init='auto', random_state=seed)
            score = silhouette_score(embeddings,model_test.fit_predict(embeddings))
            if score > best_k_with_score[1]:
                best_k_with_score = [k,score]
        return best_k_with_score[0]

def cluster_documents(corpus: Corpus, nr_labels: int, seed: int) -> ClusteredDocuments:
    lock.acquire()
    try:
        data = corpus.get_all_texts_with_ids()
        ids = list(data.keys())
        X = tfidf_vectorize(id2text=data)

        best_k: int = silhouette_analysis(nr_labels,seed,X)

        logger.info(f"Initial k-means clustering with k={best_k} started.")
        # tfidf vectorize the dataset and apply k-means++
        model = KMeans(n_clusters=best_k, init='k-means++',
                       max_iter=300, n_init='auto')
        model.fit(X)

        # get the distance to the corresponding cluster centroid for each document
        npm_tfidf = X.todense()
        centers = model.cluster_centers_
        clustered_documents: List[ClusteredDocuments] = []
        for i in range(len(ids)):
            idx = ids[i]
            tfidf_vector = npm_tfidf[i]
            distances = [norm(center - tfidf_vector) for center in centers]
            clustered_documents.append(ClusterDocument(
                idx, np.argmin(distances), np.min(distances)))
        clustered_obj = ClusteredDocuments(clustered_documents, len(centers))
        logger.info("Initial k-means clustering done.")
    finally:
        lock.release()

    return clustered_obj


def cluster_documents_with_bert_km(corpus: Corpus, nr_labels: int, seed: int) -> ClusteredDocuments:
    lock.acquire()
    try:
        X = bert_vectorize(corpus)
        best_k: int = silhouette_analysis(nr_labels,seed,X)

        logger.info(f"Initial k-means clustering with k={best_k} started.")
        # bert vectorize the dataset and apply k-means++
        model = KMeans(n_clusters=best_k, init='k-means++',
                       max_iter=300, n_init='auto')
        model.fit(X)

        # get the distance to the corresponding cluster centroid for each document
        centers = model.cluster_centers_
        clustered_documents: List[ClusteredDocuments] = []
        data = corpus.get_all_texts_with_ids()
        ids = list(data.keys())
        for i in range(len(ids)):
            id = ids[i]
            distances = [norm(center - X[i]) for center in centers]
            clustered_documents.append(ClusterDocument(
                id, np.argmin(distances), np.min(distances)))
        clustered_obj = ClusteredDocuments(clustered_documents, len(centers))
        logger.info("Initial k-means clustering done.")
    finally:
        lock.release()

    return clustered_obj


def propose_nearest_neighbors_to_centroids(clustered_docs: ClusteredDocuments, potential_ids: List[int], step_size: int,  budget: int) -> List[int]:
    """ Selects docs that are nearest to cluster centroids.
    """

    docs = clustered_docs.get_clustered_docs_by_idx(potential_ids)
    clusters = clustered_docs.clusters
    docs_per_cluster = int(step_size/len(clusters))  # equal distribution
    output_ids: List[ClusterDocument] = []
    empty_clusters = []

    for cluster in clusters:
        potential_docs_cluster = [
            doc for doc in docs if doc.cluster_idx == cluster]
        potential_docs_cluster.sort(key=lambda x: x.distance, reverse=True)
        if len(potential_docs_cluster) < docs_per_cluster:  # less docs in cluster left than needed
            output_ids.extend(potential_docs_cluster)
            empty_clusters.append(cluster)
        else:
            output_ids.extend(potential_docs_cluster[:docs_per_cluster])

    while step_size > len(output_ids) and len(empty_clusters) < len(clusters):  # rest left
        # equally distribute to not empty clusters
        docs_per_rest_clusters = int(
            (step_size-len(output_ids))/(len(clusters)-len(empty_clusters)))
        if docs_per_rest_clusters == 0:  # less than 1 per empty cluster needed for stepsize reached
            rest = step_size-len(output_ids)
            for i in range(0, rest):  # add 1 doc from first n not empty clusters (n: rest)
                not_empty_clusters = [
                    cluster for cluster in clusters if cluster not in empty_clusters]
                curr_cluster = not_empty_clusters[i]
                potential_docs_cluster = [
                    doc for doc in docs if doc.cluster_idx == curr_cluster]
                potential_docs_cluster.sort(
                    key=lambda x: x.distance, reverse=True)
                output_ids.append(potential_docs_cluster[0])

        for cluster in clusters:
            if cluster not in empty_clusters:
                potential_docs_cluster = [
                    doc for doc in docs if doc.cluster_idx == cluster]
                potential_docs_cluster.sort(
                    key=lambda x: x.distance, reverse=True)
                # less docs in cluster left than needed
                if len(potential_docs_cluster) < docs_per_rest_clusters:
                    output_ids.extend(potential_docs_cluster)
                    empty_clusters.append(cluster)
                else:
                    output_ids.extend(
                        potential_docs_cluster[:docs_per_rest_clusters])

    out_ids = [item.idx for item in output_ids]
    return out_ids


@TeacherRegistry.register("k-means")
class KMeansTeacher(BaseTeacher):

    def __init__(self, corpus: Corpus, predictor: Predictor, seed: int, labels: List[Any], nlp_task: NLPTask):
        super().__init__(
            corpus=corpus,
            predictor=predictor,
            seed=seed,
            labels=labels,
            nlp_task=nlp_task
        )
        self.k = len(self.labels)
        self.clustered_documents: ClusteredDocuments = cluster_documents(
            corpus=corpus, k=self.k, seed=seed)

    def propose(self, potential_ids: List[int], step_size: int,  budget: int) -> List[int]:
        docs = self.clustered_documents.get_clustered_docs_by_idx(
            potential_ids)
        sorted_docs = docs.sort(key=lambda x: x.distance, reverse=True)
        out_ids = [item.idx for item in sorted_docs[:step_size]]

        return out_ids


@TeacherRegistry.register("k-means-cluster-based")
class KMeansClusterBasedTeacher(BaseTeacher):
    def __init__(self, corpus: Corpus, predictor: Predictor, seed: int, labels: List[Any], nlp_task: NLPTask):
        super().__init__(
            corpus=corpus,
            predictor=predictor,
            seed=seed,
            labels=labels,
            nlp_task=nlp_task
        )
        self.nr_labels = len(self.labels)
        self.clustered_documents = cluster_documents(corpus=corpus, nr_labels=self.nr_labels, seed=seed)

    def propose(self, potential_ids: List[int], step_size: int,  budget: int) -> List[int]:
        return propose_nearest_neighbors_to_centroids(self.clustered_documents, potential_ids, step_size, budget)


@TeacherRegistry.register("k-means-cluster-based-bert-km")
class KMeansClusterBasedBERTTeacher(BaseTeacher):
    def __init__(self, corpus: Corpus, predictor: Predictor, seed: int, labels: List[Any], nlp_task: NLPTask):
        super().__init__(
            corpus=corpus,
            predictor=predictor,
            seed=seed,
            labels=labels,
            nlp_task=nlp_task
        )
        self.nr_labels = len(self.labels)
        self.clustered_documents = cluster_documents_with_bert_km(
            corpus=corpus, nr_labels=self.nr_labels, seed=seed)

    def propose(self, potential_ids: List[int], step_size: int,  budget: int) -> List[int]:
        return propose_nearest_neighbors_to_centroids(self.clustered_documents, potential_ids, step_size, budget)
