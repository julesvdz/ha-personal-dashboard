cd /volume1/docker/ha-personal-dashboard
docker build -t julesvdz/ha-personal-dashboard .
docker push julesvdz/ha-personal-dashboard:latest
cd /volume1/docker/appdata
docker-compose up -d