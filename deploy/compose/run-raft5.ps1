#-docker compose -f docker-compose-complete-5nodes.yml down --volumes --remove-orphans
docker compose -f docker-compose-complete-5nodes.yml build --no-cache
docker compose -f docker-compose-complete-5nodes.yml up -d



# Show status
docker ps