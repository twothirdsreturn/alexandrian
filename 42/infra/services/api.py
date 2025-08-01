"""FastAPI backend for 42."""

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional, Any
import uvicorn
from loguru import logger

from ..utils.config import load_config
from ..core.embedding import EmbeddingEngine
from ..core.vector_store import VectorStore
from ..core.chunker import Chunker
from .github import GitHubExtractor
from ..core.llm import LLMEngine
from ..core.cluster import ClusteringEngine


# Pydantic models
class QueryRequest(BaseModel):
    question: str
    model: str = "llama3.2"
    top_k: int = 5


class QueryResponse(BaseModel):
    question: str
    response: str
    model: str
    status: str
    prompt_length: Optional[int] = None
    error: Optional[str] = None


class ImportResponse(BaseModel):
    path: str
    chunks: int
    status: str
    error: Optional[str] = None


class ReclusterResponse(BaseModel):
    clusters: int
    status: str
    error: Optional[str] = None


class StatusResponse(BaseModel):
    qdrant: bool
    ollama: bool
    models: List[str]
    total_chunks: int
    total_clusters: int
    embedding_model: str


class LearnRequest(BaseModel):
    name: str
    url: str
    source_type: str
    domain: str
    description: str = ""


class LearnResponse(BaseModel):
    source_id: str
    name: str
    status: str
    error: Optional[str] = None


# Initialize FastAPI app
app = FastAPI(
    title="42 API",
    description="Code analysis and querying system",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Component factory functions to avoid global state
def get_config():
    """Get configuration instance."""
    return load_config()

def get_embedding_engine():
    """Get embedding engine instance."""
    return EmbeddingEngine()

def get_vector_store():
    """Get vector store instance."""
    return VectorStore()

def get_chunker():
    """Get chunker instance."""
    return Chunker()

def get_github_extractor():
    """Get GitHub extractor instance."""
    return GitHubExtractor()

def get_llm_engine():
    """Get LLM engine instance."""
    return LLMEngine()

def get_clustering_engine():
    """Get clustering engine instance."""
    return ClusteringEngine()


@app.get("/")
async def root():
    """Root endpoint."""
    return {"message": "42 API is running", "version": "1.0.0"}


@app.post("/ask", response_model=QueryResponse)
async def ask_question(request: QueryRequest):
    """Ask a question about the codebase."""
    try:
        # Get component instance
        llm_engine = get_llm_engine()
        
        result = llm_engine.query(
            request.question, 
            request.model, 
            request.top_k
        )
        
        return QueryResponse(
            question=result["question"],
            response=result["response"],
            model=result["model"],
            status=result["status"],
            prompt_length=result.get("prompt_length"),
            error=result.get("error")
        )
        
    except Exception as e:
        logger.error(f"Query failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/import/file", response_model=ImportResponse)
async def import_file(file: UploadFile = File(...)):
    """Import a single file."""
    try:
        # Read file content
        content = await file.read()
        text = content.decode('utf-8')
        
        # Get component instances  
        chunker = get_chunker()
        embedding_engine = get_embedding_engine()
        vector_store = get_vector_store()
        
        # Chunk the file
        chunks = chunker.chunk_text(text, file.filename)
        
        # Embed chunks
        vectors = []
        for chunk in chunks:
            vector = embedding_engine.embed_text(chunk.text)
            vectors.append(vector)
        
        # Store in vector database
        points = []
        for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
            from qdrant_client.models import PointStruct
            point = PointStruct(
                id=hash(f"{file.filename}_{i}") % (2**63),
                vector=vector,
                payload={
                    "text": chunk.text,
                    "file_path": file.filename,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "metadata": chunk.metadata or {}
                }
            )
            points.append(point)
        
        vector_store.upsert(points)
        
        return ImportResponse(
            path=file.filename,
            chunks=len(chunks),
            status="success"
        )
        
    except Exception as e:
        logger.error(f"File import failed: {e}")
        return ImportResponse(
            path=file.filename,
            chunks=0,
            status="error",
            error=str(e)
        )


@app.post("/import/folder", response_model=ImportResponse)
async def import_folder(path: str):
    """Import a folder recursively."""
    try:
        # Get component instances
        chunker = get_chunker()
        embedding_engine = get_embedding_engine()
        vector_store = get_vector_store()
        
        # Use the chunker to process the folder
        chunks = chunker.chunk_directory(path)
        
        if not chunks:
            return ImportResponse(
                path=path,
                chunks=0,
                status="no_chunks_found"
            )
        
        # Embed chunks in batches
        vectors = []
        batch_size = 32
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            batch_texts = [chunk.text for chunk in batch]
            batch_vectors = embedding_engine.embed_text_batch(batch_texts)
            vectors.extend(batch_vectors)
        
        # Store in vector database
        points = []
        for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
            from qdrant_client.models import PointStruct
            point = PointStruct(
                id=hash(f"{path}_{i}") % (2**63),
                vector=vector,
                payload={
                    "text": chunk.text,
                    "file_path": chunk.file_path,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "metadata": chunk.metadata or {}
                }
            )
            points.append(point)
        
        # Store in batches
        batch_size = 100
        for i in range(0, len(points), batch_size):
            batch = points[i:i + batch_size]
            vector_store.upsert(batch)
        
        return ImportResponse(
            path=path,
            chunks=len(chunks),
            status="success"
        )
        
    except Exception as e:
        logger.error(f"Folder import failed: {e}")
        return ImportResponse(
            path=path,
            chunks=0,
            status="error",
            error=str(e)
        )


@app.post("/recluster", response_model=ReclusterResponse)
async def recluster(
    min_cluster_size: int = 5,
    min_samples: int = 3
):
    """Recluster all vectors."""
    try:
        # Get component instance
        clustering_engine = get_clustering_engine()
        
        # Perform reclustering
        clusters = clustering_engine.recluster_vectors(
            min_cluster_size=min_cluster_size,
            min_samples=min_samples
        )
        
        return ReclusterResponse(
            clusters=len(clusters),
            status="success",
            error=None
        )
        
    except Exception as e:
        logger.error(f"Reclustering failed: {e}")
        return ReclusterResponse(
            clusters=0,
            status="error",
            error=str(e)
        )


@app.get("/status", response_model=StatusResponse)
async def status():
    """Get system status."""
    try:
        # Get component instances
        vector_store = get_vector_store()
        llm_engine = get_llm_engine()
        clustering_engine = get_clustering_engine()
        config = get_config()
        
        # Test Qdrant connection
        qdrant_ok = vector_store.test_connection()
        
        # Test Ollama connection
        ollama_ok = llm_engine.test_connection()
        
        # Get available models
        models = llm_engine.list_models() if ollama_ok else []
        
        # Get total chunks
        total_chunks = vector_store.get_total_points()
        
        # Get total clusters (placeholder for now)
        total_clusters = len(clustering_engine.recluster_vectors())
        
        return StatusResponse(
            qdrant=qdrant_ok,
            ollama=ollama_ok,
            models=models,
            total_chunks=total_chunks,
            total_clusters=total_clusters,
            embedding_model=config.embedding_model
        )
        
    except Exception as e:
        logger.error(f"Status check failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/search")
async def search(query: str, limit: int = 10):
    """Search for similar code patterns."""
    try:
        # Get component instance
        github_extractor = get_github_extractor()
        
        results = github_extractor.search_patterns(query, limit)
        return {"results": results}
        
    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def run_server(host: str = "0.0.0.0", port: int = 8000):
    """Run the FastAPI server."""
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_server() 