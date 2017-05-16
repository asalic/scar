SCAR - Serverless Container-aware ARchitectures
===============================================

SCAR is a framework to transparently execute containers (e.g. Docker) in serverless platforms (e.g. AWS Lambda) to create ultra-elastic application architectures in the Cloud.

## Approach

SCAR uses the following underlying technologies:

* [udocker](https://github.com/indigo-dc/udocker/): A tool to execute Docker containers in user space.
* [AWS Lambda](https://aws.amazon.com/lambda): A serverless compute service that runs Lambda functions in response to events.

SCAR creates Lambda functions and provides a command-line interface to transparently execute Docker containers in AWS Lambda.

## Licensing
SCAR is licensed under the Apache License, Version 2.0. See
[LICENSE](https://github.com/grycap/scar/blob/master/LICENSE) for the full
license text.