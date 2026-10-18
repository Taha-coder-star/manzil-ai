# Fresh-search release: cost-saving shutdown and bring-back (separate from the deployment)

Run only **after** the smoke checks in `docs/RELEASE_FRESH_SEARCH.md` section 9 are finished and the
release is accepted (or rolled back). The AWS budget is $10. Nothing here is part of the deployment
procedure and nothing in the deployment depends on it.

Fill `REGION`, `EC2_ID`, `RDS_ID`, `<bucket>` and `<SITE_ADDRESS>` from what you verified (do not guess):

```
aws ec2 describe-instances --region <REGION> --query 'Reservations[].Instances[].[InstanceId,State.Name,PublicIpAddress]' --output table
aws rds describe-db-instances --region <REGION> --query 'DBInstances[].[DBInstanceIdentifier,DBInstanceStatus]' --output table
```

Snapshots: none were made for this release. Delete a snapshot only by name, with the owner's explicit yes, and
never the latest automated backup.

## A. Shut down

```
REGION=<REGION>; EC2_ID=<i-...>; RDS_ID=<db-identifier>
aws s3 rm s3://<bucket>/staging/release-fresh/ --recursive          # staging prefix only
aws rds describe-db-snapshots --db-instance-identifier $RDS_ID --region $REGION --query 'DBSnapshots[].[DBSnapshotIdentifier,SnapshotCreateTime,SnapshotType]' --output table
# aws rds delete-db-snapshot --db-snapshot-identifier <NAMED_SNAPSHOT> --region $REGION    # only a named release snapshot, if one exists
aws ec2 stop-instances --instance-ids $EC2_ID --region $REGION
aws rds stop-db-instance --db-instance-identifier $RDS_ID --region $REGION
```

Do not stop the servers before the rollback decision is final: a rollback needs them running.

## B. Bring production back

```
aws rds start-db-instance --db-instance-identifier $RDS_ID --region $REGION
aws rds wait db-instance-available --db-instance-identifier $RDS_ID --region $REGION
aws ec2 start-instances --instance-ids $EC2_ID --region $REGION
aws ec2 wait instance-running --instance-ids $EC2_ID --region $REGION
aws ec2 describe-instances --instance-ids $EC2_ID --region $REGION --query 'Reservations[].Instances[].PublicIpAddress'
# on the instance:
cd /home/ubuntu/manzil-ai && docker compose -f deploy/compose.yml --env-file .env up -d web worker caddy
docker compose -f deploy/compose.yml --env-file .env ps
curl -fsS https://<SITE_ADDRESS>/health
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<SITE_ADDRESS>/webhook -d '{}'      # 401 = the route is alive
```

## Cautions

- A stopped RDS instance **restarts by itself after 7 days**.
- A stopped EC2 instance gets a **new public IP** unless an Elastic IP is attached; the Meta webhook URL / DNS depends on it.
  Re-check that `<SITE_ADDRESS>` resolves to the new IP before testing.
- Confirm the $10 budget alert exists in the AWS console.
