#!/bin/bash

if [ "${EXEC_TYPE,,}" = 'lambda' ]; then
  export OMPI_MCA_plm_rsh_agent=/bin/false
  mpirun ${MPI_PARAMS} ${APP_BIN} ${APP_PARAMS}

elif [ "${EXEC_TYPE,,}" = 'batch' ]; then

# The following comment line will be replaced with the necessary env vars:
#=ENV_VARS=

  export AWS_BATCH_EXIT_CODE_FILE=~/batch_exit_code.file
  echo "Running on node index $AWS_BATCH_JOB_NODE_INDEX out of $AWS_BATCH_JOB_NUM_NODES nodes"
  echo "Master node index is $AWS_BATCH_JOB_MAIN_NODE_INDEX and its IP is $AWS_BATCH_JOB_MAIN_NODE_PRIVATE_IPV4_ADDRESS"

  #wget -q -P /tmp --no-check-certificate --no-proxy 'http://scar-architrave.s3.amazonaws.com/awscli-exe-linux-x86_64.zip'
  wget -P /tmp https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip
  echo "Download awscli complete"
  7z x -aoa -o/tmp/ /tmp/awscli-exe-linux-x86_64.zip
  chmod +x /tmp/aws/install
  /tmp/aws/install
  echo "Version of dist: ${VERSION}"
  mkdir ~/.aws/
  ## S3 OPTIMIZATION
  aws configure set default.s3.max_concurrent_requests 30
  aws configure set default.s3.max_queue_size 10000
  aws configure set default.s3.multipart_threshold 64MB
  aws configure set default.s3.multipart_chunksize 16MB
  aws configure set default.s3.max_bandwidth 4096MB/s
  aws configure set default.s3.addressing_style path
  printf '%s\n' '[default]' "aws_access_key_id=${AWS_ACCESS_KEY}" "aws_secret_access_key=${AWS_SECRET_ACCESS_KEY}" > ~/.aws/credentials
  printf '%s\n' '[default]' "region=${AWS_REGION}" "output=${AWS_OUTPUT}" > ~/.aws/config
  #aws s3 cp $S3_INPUT/common $SCRATCH_DIR
  echo "Install batch only dependencies from S3"
  mkdir ${SCRATCH_DIR}
  mkdir ${JOB_DIR}
  aws s3 cp ${S3_BUCKET}/${S3_BATCH_DEPS_REL_PATH} /tmp
  tar -zxf /tmp/deps.tar.gz -C /tmp
  dpkg -i /tmp/*.deb

  echo "Add private data from S3"
  #rm -rf /tmp/*
  aws s3 cp ${S3_BUCKET}/${S3_BATCH_PRIVATE_REL_PATH} /tmp
  7z x -aoa -p${PRIVATE_PASSWD} -o/opt /tmp/*.7z

  echo "Configure ssh"
  sed 's@session\s*required\s*pam_loginuid.so@session optional pam_loginuid.so@g' -i /etc/pam.d/sshd
  echo "export VISIBLE=now" >> /etc/profile
  echo "${USER} ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
  mkdir -p ${SSHDIR}
  touch ${SSHDIR}/sshd_config
  ssh-keygen -t rsa -f ${SSHDIR}/ssh_host_rsa_key -N ''
  cp ${SSHDIR}/ssh_host_rsa_key.pub ${SSHDIR}/authorized_keys
  cp ${SSHDIR}/ssh_host_rsa_key ${SSHDIR}/id_rsa
  echo " IdentityFile ${SSHDIR}/id_rsa" >> /etc/ssh/ssh_config
  echo "Host *" >> /etc/ssh/ssh_config
  echo " StrictHostKeyChecking no" >> /etc/ssh/ssh_config
  chmod -R 600 ${SSHDIR}/*
  chown -R ${USER}:${USER} ${SSHDIR}/
    # check if ssh agent is running or not, if not, run
  eval `ssh-agent -s`
  ssh-add ${SSHDIR}/id_rsa

  chmod +x ${APP_BIN}

  echo "Running app"
  /opt/mpi-run.sh
else
  echo "ERROR: unknown execution type '${EXEC_TYPE}'"
  exit 1 # terminate and indicate error
fi