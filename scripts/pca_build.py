#!/usr/bin/env python3
"""
PCA Basis Generation Script for Met Gallery Backend

This script computes PCA components from the image embeddings stored in the database
and saves them to pca_basis.json for use by the field-chunk endpoint.

Requirements:
- PostgreSQL with pgvector extension
- Python with psycopg2, numpy, sklearn
- Database with embeddings already populated

Usage:
    python scripts/pca_build.py
"""

import os
import json
import numpy as np
import psycopg2
from sklearn.decomposition import IncrementalPCA
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def get_db_connection():
    """Create a database connection using DATABASE_URL."""
    database_url = os.getenv('DATABASE_URL')
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is required")
    return psycopg2.connect(database_url)

def fetch_embeddings_batch(cursor, batch_size=8192, offset=0):
    """Fetch a batch of embeddings from the database."""
    query = """
        SELECT "imgVec" 
        FROM "met-galaxy_artwork" 
        WHERE "imgVec" IS NOT NULL 
        AND "localImageUrl" IS NOT NULL 
        AND "localImageUrl" != ''
        ORDER BY id
        LIMIT %s OFFSET %s
    """
    cursor.execute(query, (batch_size, offset))
    results = cursor.fetchall()
    
    if not results:
        return None
        
    # Convert to numpy array
    embeddings = []
    for row in results:
        if row[0] is not None:
            # Parse string representation of vector
            if isinstance(row[0], str):
                # Remove brackets and split by comma
                vector_str = row[0].strip('[]')
                vector_values = [float(x) for x in vector_str.split(',')]
                embeddings.append(vector_values)
            else:
                # Already a list/array
                embeddings.append(row[0])
    
    return np.array(embeddings, dtype=np.float32) if embeddings else None

def count_total_embeddings(cursor):
    """Count total number of valid embeddings."""
    query = """
        SELECT COUNT(*) 
        FROM "met-galaxy_artwork" 
        WHERE "imgVec" IS NOT NULL 
        AND "localImageUrl" IS NOT NULL 
        AND "localImageUrl" != ''
    """
    cursor.execute(query)
    return cursor.fetchone()[0]

def main():
    print("🔄 Starting PCA basis generation...")
    
    try:
        # Connect to database
        print("📡 Connecting to database...")
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Count total embeddings
        total_count = count_total_embeddings(cursor)
        print(f"📊 Found {total_count} valid embeddings in database")
        
        if total_count == 0:
            print("❌ No embeddings found! Make sure to run generate-embeddings.py first.")
            return
            
        # Initialize IncrementalPCA
        print("🧮 Initializing PCA with 4 components...")
        pca = IncrementalPCA(n_components=4, batch_size=8192)
        
        # Process embeddings in batches
        batch_size = 8192
        offset = 0
        processed = 0
        
        print("⚡ Processing embeddings in batches...")
        while offset < total_count:
            print(f"  Processing batch {offset//batch_size + 1}: items {offset} to {min(offset + batch_size, total_count)}")
            
            # Fetch batch
            batch = fetch_embeddings_batch(cursor, batch_size, offset)
            if batch is None or len(batch) == 0:
                break
                
            # L2 normalize embeddings (recommended for CLIP embeddings)
            norms = np.linalg.norm(batch, axis=1, keepdims=True)
            batch_normalized = batch / (norms + 1e-12)
            
            # Fit PCA incrementally
            pca.partial_fit(batch_normalized)
            
            processed += len(batch)
            offset += batch_size
            
        print(f"✅ Processed {processed} embeddings")
        
        # Get PCA components and normalize them
        print("🎯 Extracting and normalizing PCA components...")
        U = pca.components_.astype(np.float32)
        U_normalized = U / (np.linalg.norm(U, axis=1, keepdims=True) + 1e-12)
        
        # Save to JSON file
        output_path = "pca_basis.json"
        pca_data = {
            "basis": U_normalized.tolist(),
            "explained_variance_ratio": pca.explained_variance_ratio_.tolist(),
            "n_samples": processed,
            "n_components": len(U_normalized),
            "embedding_dim": U_normalized.shape[1]
        }
        
        print(f"💾 Saving PCA basis to {output_path}...")
        with open(output_path, "w") as f:
            json.dump(pca_data, f, indent=2)
            
        print("✅ PCA basis generation completed!")
        print(f"   Components: {len(U_normalized)}")
        print(f"   Embedding dimension: {U_normalized.shape[1]}")
        print(f"   Explained variance ratios: {[f'{r:.3f}' for r in pca.explained_variance_ratio_]}")
        print(f"   Total samples processed: {processed}")
        
        # Close database connection
        cursor.close()
        conn.close()
        
    except Exception as e:
        print(f"❌ Error during PCA generation: {e}")
        raise

if __name__ == "__main__":
    main()