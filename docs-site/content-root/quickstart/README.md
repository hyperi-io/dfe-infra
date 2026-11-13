# Quickstarts

Four ways in, each a handful of commands. Start with the demo if you have not seen DFE work yet.

| You want to | Page | Runs on |
|---|---|---|
| See it parse live firewall syslog | [The demo](demo.md) | One host, Docker Compose |
| Send it your own data on one host | [Docker](docker.md) | One host, Docker Compose |
| Run it on a cluster you own | [Kubernetes on-premises](kubernetes.md) | Kubernetes, in the supported range |
| Run it on Amazon EKS | [AWS](aws.md) | EKS, built by OpenTofu |

## How long a full cycle takes

Standing DFE up on a cloud, testing it and pulling it down again takes a while:

| Target | Full cycle |
|---|---|
| AWS, default backends | about 2 hours |
| AWS with Amazon MSK | a lot longer |

Your mileage will vary. The region, how busy the cloud is that day, CI queues and which backends you turn on all move it. Plan on the rough figure, not to the minute.
