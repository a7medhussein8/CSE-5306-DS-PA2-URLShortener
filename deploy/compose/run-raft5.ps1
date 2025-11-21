# -------------------------------------------
#   Cleanup (optional but recommended)
# -------------------------------------------

Write-Host "Stopping running containers..."
docker ps -q | ForEach-Object { docker stop $_ } 2>$null

Write-Host "Removing all containers..."
docker ps -aq | ForEach-Object { docker rm $_ } 2>$null

Write-Host "Removing unused networks..."
docker network prune -f

Write-Host "Removing unused volumes..."
docker volume prune -f

Write-Host "Removing unused images..."
docker image prune -af

# -------------------------------------------
#   Run Raft Cluster + Gateway + Services
# -------------------------------------------

Write-Host "Building Raft 5-node cluster..."
docker-compose -f docker-compose-complete-5nodes.yml build

Write-Host "Starting Raft 5-node cluster..."
docker-compose -f docker-compose-complete-5nodes.yml up -d

Write-Host "All services are now running!"
docker ps
