

## frist need to be in the path below then you can run the clean up and bulid script using run-raft.ps1 file 

D:\UTA\URL\CSE-5306-DS-PA2-URLShortener\deploy\compose>

## command to run from the above dierctory from same directory above

.\run-raft5.ps1


## to check raft status use the command from same directory above

.\show-raft-status.ps1
**Run the Script Inside the Container-powershell# Execute the script inside the container**

docker exec -it ratelimit-1 python /app/debug_grpc.py


**What this does:** Runs Python inside the container and executes the debug script
