# 🐳 Docker Deployment Guide

## Quick Start

1. **Build the Docker image:**
   ```bash
   docker build -t met-galaxy-backend .
   ```

2. **Run locally with Docker:**
   ```bash
   docker run -p 8080:8080 \
     -e NODE_ENV=production \
     -e DATABASE_URL="your_neon_database_url" \
     -e AWS_ACCESS_KEY_ID="your_aws_key" \
     -e AWS_SECRET_ACCESS_KEY="your_aws_secret" \
     -e CORS_ORIGINS="https://your-frontend.com" \
     met-galaxy-backend
   ```

## Deployment Platforms

### 🚄 Railway (Recommended)
1. Connect your GitHub repo to Railway
2. Add environment variables in Railway dashboard
3. Railway will auto-build and deploy from Dockerfile

**Environment variables needed:**
- `DATABASE_URL` - Your Neon PostgreSQL connection string
- `AWS_ACCESS_KEY_ID` - AWS access key for S3
- `AWS_SECRET_ACCESS_KEY` - AWS secret key
- `CORS_ORIGINS` - Comma-separated frontend URLs

### ✈️ Fly.io
```bash
# Install flyctl and login
fly launch
fly secrets set DATABASE_URL="your_neon_url"
fly secrets set AWS_ACCESS_KEY_ID="your_key"
fly secrets set AWS_SECRET_ACCESS_KEY="your_secret"
fly secrets set CORS_ORIGINS="https://your-frontend.com"
fly deploy
```

### ☁️ Google Cloud Run
```bash
# Build and push to Google Container Registry
gcloud builds submit --tag gcr.io/PROJECT_ID/met-galaxy-backend
gcloud run deploy --image gcr.io/PROJECT_ID/met-galaxy-backend --platform managed
```

### 🌊 AWS ECS/Fargate
1. Push image to ECR
2. Create ECS task definition with environment variables
3. Deploy to ECS service

## Environment Variables

Copy `.env.example` to set up your production environment:

```bash
NODE_ENV=production
PORT=8080
DATABASE_URL=postgresql://user:pass@host:port/db?sslmode=require
AWS_ACCESS_KEY_ID=your_aws_access_key_id
AWS_SECRET_ACCESS_KEY=your_aws_secret_access_key
CORS_ORIGINS=https://your-frontend.com,https://admin.your-site.com
```

## Health Check

The container includes a health check endpoint at `/health`. Most platforms will automatically use this to monitor your service.

## Performance Notes

- ✅ **Partial HNSW index** already applied for vector similarity queries
- ✅ **PCA basis file** included in container for field-chunk API
- ✅ **Multi-stage build** for smaller production image (~150MB)
- ✅ **Non-root user** for security

## Troubleshooting

**Container fails to start:**
- Check DATABASE_URL is correct and accessible
- Ensure pca_basis.json exists (should be automatic)

**API returns CORS errors:**
- Verify CORS_ORIGINS matches your frontend domain
- Include protocol (https://) in CORS_ORIGINS

**Slow vector similarity queries:**
- Ensure database migration was applied: `npm run db:migrate`
- Check index exists: `\d+ "met-galaxy_artwork"` in psql