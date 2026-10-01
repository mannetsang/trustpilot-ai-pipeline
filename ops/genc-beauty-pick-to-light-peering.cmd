@echo off
REM Pick-to-light ("find it") from the GenC Beauty dashboard.
REM
REM The light-strip bridge runs on the mqtt-broker VM in the OLD project
REM (shp-ai-bot-2026, default VPC, private IP 10.128.0.2, port 8080). The new
REM dashboard in genc-beauty has no network, so it cannot reach that address.
REM This script peers a small VPC in genc-beauty with the old project's default
REM VPC, opens tcp:8080 on the VM to that subnet, and attaches the dashboard.
REM Every step is skipped when it already exists, so the script can be re-run.
REM
REM Run once from a cmd prompt where `gcloud auth login` is done as an owner of
REM BOTH projects. Claude's automation is not allowed to apply network changes.
setlocal
set OLD=shp-ai-bot-2026
set NEW=genc-beauty
set R=us-central1

echo [1/6] VPC genc-vpc in %NEW%
call gcloud compute networks describe genc-vpc --project %NEW% >nul 2>&1 || call gcloud compute networks create genc-vpc --project %NEW% --subnet-mode custom --quiet || goto :fail

echo [2/6] subnet genc-run-us-central1 10.64.0.0/24 (outside the old auto range 10.128.0.0/9)
call gcloud compute networks subnets describe genc-run-us-central1 --project %NEW% --region %R% >nul 2>&1 || call gcloud compute networks subnets create genc-run-us-central1 --project %NEW% --network genc-vpc --region %R% --range 10.64.0.0/24 --quiet || goto :fail

echo [3/6] peering genc-vpc -^> %OLD%/default
call gcloud compute networks peerings list --project %NEW% --network genc-vpc --format="value(peerings[].name)" | findstr /c:"genc-to-shp" >nul || call gcloud compute networks peerings create genc-to-shp --project %NEW% --network genc-vpc --peer-project %OLD% --peer-network default --quiet || goto :fail

echo [4/6] peering %OLD%/default -^> genc-vpc
call gcloud compute networks peerings list --project %OLD% --network default --format="value(peerings[].name)" | findstr /c:"shp-to-genc" >nul || call gcloud compute networks peerings create shp-to-genc --project %OLD% --network default --peer-project %NEW% --peer-network genc-vpc --quiet || goto :fail

echo [5/6] old-project firewall: tcp:8080 from 10.64.0.0/24 to the mqtt-broker VM
call gcloud compute firewall-rules describe allow-light-bridge-genc --project %OLD% >nul 2>&1 || call gcloud compute firewall-rules create allow-light-bridge-genc --project %OLD% --network default --direction INGRESS --action allow --rules tcp:8080 --source-ranges 10.64.0.0/24 --target-tags mqtt-broker --description "GenC Beauty dashboard (genc-beauty/genc-vpc, peered) -> pick-to-light bridge" --quiet || goto :fail

echo [6/6] new dashboard: Direct VPC egress through the peered subnet + LIGHT_BRIDGE_URL
call gcloud run services update genc-sales-dashboard --region %R% --project %NEW% --network genc-vpc --subnet genc-run-us-central1 --vpc-egress private-ranges-only --update-env-vars LIGHT_BRIDGE_URL=http://10.128.0.2:8080 --quiet || goto :fail

echo.
echo Done. Peering state (both lines should say ACTIVE):
call gcloud compute networks peerings list --project %NEW% --network genc-vpc --format="value(peerings[].name,peerings[].state)"
call gcloud compute networks peerings list --project %OLD% --network default --format="value(peerings[].name,peerings[].state)"
echo Now open the GenC dashboard, Delivery board, and click "find it" on a line: the tag should light.
exit /b 0

:fail
echo FAILED at the step above. Fix the cause and re-run; finished steps are skipped.
exit /b 1
