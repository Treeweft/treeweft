"""Dependency injection wiring: create adapter instances and inject into domain services.

This module creates all adapter instances and injects them into domain services.
Used by the application layer to bootstrap the system.
"""
from treeweft.domain.indexing import EmbeddingPort, IndexingService
from treeweft.domain.search import SearchService, CommunityPort, GraphSearchPort
from treeweft.adapters.milvus.vector_store import MilvusVectorStoreAdapter
from treeweft.adapters.tei.embedding_proxy import TEIEmbeddingAdapter
from treeweft.adapters.tei.reranker import TEIRerankerAdapter
from treeweft.adapters.neo4j.graph_store import Neo4jGraphStoreAdapter
from treeweft.adapters.llm_api.llm_adapter import LLMAdapter
from treeweft.adapters.graph.community_adapter import CommunityAdapter
from treeweft.application.search import SearchService as SearchServiceImpl
from treeweft.application.indexer_service import IndexerService as IndexerServiceImpl


def create_dependencies() -> dict:
    """Create all adapter instances and return as DI container."""
    return {
        "embedding_port": TEIEmbeddingAdapter(),
        "rerank_port": TEIRerankerAdapter(),
        "vector_store": MilvusVectorStoreAdapter(),
        "graph_store": Neo4jGraphStoreAdapter(),
        "llm": LLMAdapter(),
        "community": CommunityAdapter(),
    }


def create_search_service(deps: dict | None = None) -> SearchService:
    """Create SearchService with dependency injection."""
    if deps is None:
        deps = create_dependencies()
    return SearchServiceImpl(
        vector_store=deps["vector_store"],
        rerank_port=deps["rerank_port"],
        graph_search_port=deps["graph_store"],
        community_port=deps["community"],
        llm_port=deps["llm"],
        embedding_port=deps["embedding_port"],
    )


def create_indexer_service(deps: dict | None = None) -> IndexingService:
    """Create IndexerService with dependency injection."""
    if deps is None:
        deps = create_dependencies()
    return IndexerServiceImpl(
        embedding_port=deps["embedding_port"],
        graph_store=deps["graph_store"],
    )
