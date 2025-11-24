# update-code.ps1

Write-Host "Stopping containers..."
docker compose -f docker-compose-complete-5nodes.yml down

Write-Host "Building all services (using cache for pip/deps)..."
docker compose -f docker-compose-complete-5nodes.yml build

Write-Host "Starting updated containers..."
docker compose -f docker-compose-complete-5nodes.yml up -d

Write-Host "Showing running containers..."
docker compose -f docker-compose-complete-5nodes.yml ps
