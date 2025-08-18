#!/usr/bin/env python3
"""
Generate CLIP image embeddings for Met Museum artworks.

This script:
1. Fetches artworks with localImageUrl but no imgVec
2. Downloads images from S3
3. Generates CLIP embeddings using OpenCLIP ViT-L/14
4. Stores embeddings in PostgreSQL as vectors

Usage:
    python scripts/generate-embeddings.py
"""

import os
import sys
import time
import requests
import psycopg2
import torch
import numpy as np
from PIL import Image
from io import BytesIO
import open_clip
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Debug: Check if DATABASE_URL is loaded
database_url = os.getenv('DATABASE_URL')
if not database_url:
    print("❌ DATABASE_URL not found in environment variables")
    print("Make sure your .env file contains DATABASE_URL=...")
    sys.exit(1)
elif database_url.startswith('postgresql://'):
    print("✅ DATABASE_URL loaded successfully (Neon connection)")
else:
    print(f"⚠️  DATABASE_URL format: {database_url[:30]}...")

# Configuration
BATCH_SIZE = 10  # Process N images at once
MODEL_NAME = "ViT-L-14"
PRETRAINED = "openai"  # High-quality pretrained weights
# In your embedding script, change this line:
DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"
# DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

print(f"🚀 Starting CLIP embedding generation...")
print(f"⚙️  Device: {DEVICE}")
print(f"⚙️  Model: {MODEL_NAME}-{PRETRAINED}")
print(f"⚙️  Batch size: {BATCH_SIZE}")

def load_clip_model():
    """Load CLIP model and preprocessing."""
    print(f"📥 Loading CLIP model: {MODEL_NAME}-{PRETRAINED}...")
    model, _, preprocess = open_clip.create_model_and_transforms(
        MODEL_NAME, 
        pretrained=PRETRAINED,
        device=DEVICE
    )
    model.eval()
    print(f"✅ Model loaded successfully (device: {DEVICE})")
    return model, preprocess

def download_image(url):
    """Download image from URL and return PIL Image."""
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        image = Image.open(BytesIO(response.content))
        # Convert to RGB if needed (handles RGBA, grayscale, etc.)
        if image.mode != 'RGB':
            image = image.convert('RGB')
        return image
    except Exception as e:
        raise Exception(f"Failed to download/process image: {str(e)}")

def generate_embedding(model, preprocess, image):
    """Generate CLIP embedding for a single image."""
    try:
        # Preprocess image
        image_tensor = preprocess(image).unsqueeze(0).to(DEVICE)
        
        # Generate embedding
        with torch.no_grad():
            image_features = model.encode_image(image_tensor)
            # Normalize the embedding
            image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        
        # Convert to numpy array
        embedding = image_features.cpu().numpy().flatten()
        return embedding.tolist()
    except Exception as e:
        raise Exception(f"Failed to generate embedding: {str(e)}")

def process_batch(model, preprocess, artworks, cursor):
    """Process a batch of artworks."""
    print(f"🚀 Processing batch of {len(artworks)} artworks...")
    
    results = []
    for artwork in artworks:
        artwork_id = artwork[0]
        image_url = artwork[1]
        
        try:
            print(f"📥 Processing artwork {artwork_id}: {image_url}")
            
            # Download image
            image = download_image(image_url)
            
            # Generate embedding
            embedding = generate_embedding(model, preprocess, image)
            
            # Update database
            cursor.execute(
                'UPDATE "met-galaxy_artwork" SET "imgVec" = %s WHERE id = %s',
                (embedding, artwork_id)
            )
            
            print(f"✅ Success: {artwork_id} -> embedding generated ({len(embedding)} dims)")
            results.append({"success": True, "id": artwork_id})
            
        except Exception as e:
            print(f"❌ Failed: {artwork_id} - {str(e)}")
            results.append({"success": False, "id": artwork_id, "error": str(e)})
    
    return results

def main():
    try:
        # Connect to database
        database_url = os.getenv('DATABASE_URL')
        if not database_url:
            print("❌ DATABASE_URL not found. Make sure to run: source .env && python script")
            sys.exit(1)
        
        # Clean up Neon connection string (remove problematic channel_binding)
        if 'channel_binding=require' in database_url:
            database_url = database_url.replace('channel_binding=require', '')
            database_url = database_url.rstrip('&')  # Remove trailing &
        
        conn = psycopg2.connect(database_url)
        cursor = conn.cursor()
        print("🔌 Connected to database")
        
        # Load CLIP model
        model, preprocess = load_clip_model()
        
        # Get artworks that need embeddings
        cursor.execute("""
            SELECT id, "localImageUrl" 
            FROM "met-galaxy_artwork" 
            WHERE "localImageUrl" IS NOT NULL 
              AND "localImageUrl" != ''
              AND "imgVec" IS NULL
            ORDER BY id
            LIMIT 1000
        """)
        
        artworks = cursor.fetchall()
        print(f"🎯 Found {len(artworks)} artworks to process")
        
        if len(artworks) == 0:
            print("🎉 No artworks to process - all done!")
            return
        
        total_successes = 0
        total_failures = 0
        start_time = time.time()
        
        # Process in batches
        for i in range(0, len(artworks), BATCH_SIZE):
            batch = artworks[i:i + BATCH_SIZE]
            batch_num = i // BATCH_SIZE + 1
            total_batches = (len(artworks) + BATCH_SIZE - 1) // BATCH_SIZE
            
            print(f"\n📊 Batch {batch_num}/{total_batches}")
            
            batch_start = time.time()
            results = process_batch(model, preprocess, batch, cursor)
            batch_end = time.time()
            
            # Commit after each batch
            conn.commit()
            
            successes = sum(1 for r in results if r["success"])
            failures = sum(1 for r in results if not r["success"])
            
            total_successes += successes
            total_failures += failures
            
            batch_duration = batch_end - batch_start
            rate = len(batch) / batch_duration if batch_duration > 0 else 0
            
            print(f"📊 Batch {batch_num} complete: {successes}/{len(batch)} successful")
            print(f"⏱️  Batch time: {batch_duration:.1f}s ({rate:.1f} imgs/sec)")
            print(f"📈 Total progress: {total_successes} successes, {total_failures} failures")
            
            # Small delay between batches to be nice
            if i + BATCH_SIZE < len(artworks):
                time.sleep(0.5)
        
        total_time = time.time() - start_time
        overall_rate = len(artworks) / total_time if total_time > 0 else 0
        
        print(f"\n🎉 Embedding generation complete!")
        print(f"📊 Final stats: {total_successes} successes, {total_failures} failures")
        print(f"💯 Success rate: {(total_successes / len(artworks) * 100):.1f}%")
        print(f"⏱️  Total time: {total_time:.1f}s ({overall_rate:.1f} imgs/sec)")
        
    except Exception as e:
        print(f"💥 Fatal error: {e}")
        sys.exit(1)
    finally:
        if 'conn' in locals():
            conn.close()
            print("🔌 Database connection closed")

if __name__ == "__main__":
    main()
