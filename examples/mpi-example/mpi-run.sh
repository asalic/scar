#!/bin/bash

cd $JOB_DIR

#PATH="$PATH:/opt/openmpi/bin/"
BASENAME="${0##*/}"
log () {
  echo "${BASENAME} - ${1}"
}
HOST_FILE_PATH="/tmp/hostfile"
AWS_BATCH_EXIT_CODE_FILE="/tmp/batch-exit-code"

#aws s3 cp $S3_INPUT $SCRATCH_DIR
#tar -xvf $SCRATCH_DIR/*.tar.gz -C $SCRATCH_DIR

sleep 2

usage () {
  if [ "${#@}" -ne 0 ]; then
    log "* ${*}"
    log
  fi
  cat <<ENDUSAGE
Usage:
export AWS_BATCH_JOB_NODE_INDEX=0
export AWS_BATCH_JOB_NUM_NODES=10
export AWS_BATCH_JOB_MAIN_NODE_INDEX=0
export AWS_BATCH_JOB_ID=string
./mpi-run.sh
ENDUSAGE

  error_exit
}

# Standard function to print an error and exit with a failing return code
error_exit () {
  log "${BASENAME} - ${1}" >&2
  log "${2:-1}" > $AWS_BATCH_EXIT_CODE_FILE
  kill  $(cat /tmp/supervisord.pid)
}

# Set child by default switch to main if on main node container
NODE_TYPE="child"
if [ "${AWS_BATCH_JOB_MAIN_NODE_INDEX}" == "${AWS_BATCH_JOB_NODE_INDEX}" ]; then
  log "Running synchronize as the main node"
  NODE_TYPE="main"
fi


# wait for all nodes to report
wait_for_nodes () {
  log "Running as master node"

  touch $HOST_FILE_PATH
  ip=$(/sbin/ip -o -4 addr list eth0 | awk '{print $4}' | cut -d/ -f1)

  if [ -x "$(command -v nvidia-smi)" ] ; then
      NUM_GPUS=$(ls -l /dev/nvidia[0-9] | wc -l)
      availablecores=$NUM_GPUS
  else
      availablecores=$(nproc)
  fi

  log "master details -> $ip:$availablecores"
  echo "$ip slots=$availablecores" >> $HOST_FILE_PATH

  lines=$(sort $HOST_FILE_PATH|uniq|wc -l)
  while [ "$AWS_BATCH_JOB_NUM_NODES" -gt "$lines" ]
  do
    log "$lines out of $AWS_BATCH_JOB_NUM_NODES nodes joined, check again in 1 second"
    sleep 1
    lines=$(sort $HOST_FILE_PATH|uniq|wc -l)
  done
  # Make the temporary file executable and run it with any given arguments
  log "All nodes successfully joined"

  # remove duplicates if there are any.
  awk '!a[$0]++' $HOST_FILE_PATH > ${HOST_FILE_PATH}-deduped
  cat $HOST_FILE_PATH-deduped
  log "executing main MPIRUN workflow"

  cd $SCRATCH_DIR
  mkdir output
  # --allow-run-as-root
  { time  mpirun --mca btl_tcp_if_include eth0 --debug-daemons -x PATH -x LD_LIBRARY_PATH --machinefile ${HOST_FILE_PATH}-deduped \
      ${APP_BIN} ${APP_PARAMS} }; } 2>&1 | cat > ${S3_BATCH_MNT}/output/time.log
  sleep 2
  echo 'Exec output:'
  cat ${S3_BATCH_MNT}/output/time.log

  #if [ "${NODE_TYPE}" = 'main' ]; then
    # env GZIP=-9 tar -czvf $SCRATCH_DIR/batch_output_${AWS_BATCH_JOB_ID}.tar.gz $SCRATCH_DIR/output/*
    # aws s3 cp $SCRATCH_DIR/batch_output_${AWS_BATCH_JOB_ID}.tar.gz $S3_BUCKET/output/batch_output_${AWS_BATCH_JOB_ID}.tar.gz
  #fi

  log "done! goodbye, writing exit code to $AWS_BATCH_EXIT_CODE_FILE and shutting down my supervisord"
  echo "0" > $AWS_BATCH_EXIT_CODE_FILE
  kill  $(cat /tmp/supervisord.pid)
  #echo "#!/bin/bash" > ${S3_BATCH_MNT}/exec/docker_done
  #echo "env GZIP=-9 tar -czvf /mnt/batch/output/result.tar.gz /mnt/batch/output/*" > ${S3_BATCH_MNT}/exec/docker_done
  #echo "/usr/local/bin/aws s3 cp /mnt/batch/output/result.tar.gz s3://scar-architrave/output/result_$(date | tr ' ' _ ).tar.gz" >> ${S3_BATCH_MNT}/exec/docker_done
  #log "Signaling children to exit"
  #cat ${HOST_FILE_PATH}-deduped | awk -F_ '{print $1}' | xargs -I{} -n1 ssh {} "touch /mnt/batch/mpi/master_done"
  #while inotifywait ${S3_BATCH_MNT}/exec -e create; do { echo "EC2 host post-execution process completed, exiting container"; break; }; done
  exit 0
}


# Fetch and run a script
report_to_master () {
  # get own ip and num cpus
  #
  ip=$(/sbin/ip -o -4 addr list eth0 | awk '{print $4}' | cut -d/ -f1)

  if [ -x "$(command -v nvidia-smi)" ] ; then
      NUM_GPUS=$(ls -l /dev/nvidia[0-9] | wc -l)
      availablecores=$NUM_GPUS
  else
      availablecores=$(nproc)
  fi

  log "I am a child node -> $ip:$availablecores, reporting to the master node -> ${AWS_BATCH_JOB_MAIN_NODE_PRIVATE_IPV4_ADDRESS}"
  until echo "$ip slots=$availablecores" | ssh ${AWS_BATCH_JOB_MAIN_NODE_PRIVATE_IPV4_ADDRESS} "cat >> /$HOST_FILE_PATH"
  do
    echo "Sleeping 5 seconds and trying again"
  done
  #touch ${S3_BATCH_MNT}/exec/docker_done

  #echo "Wait for master to finish"
  #while inotifywait ${S3_BATCH_MNT}/mpi -e create; do { echo "Master has finished its execution, done! goodbye"; break; }; done
  exit 0
}


# Main - dispatch user request to appropriate function
log $NODE_TYPE
case $NODE_TYPE in
  main)
    wait_for_nodes "${@}"
    ;;

  child)
    report_to_master "${@}"
    ;;

  *)
    log $NODE_TYPE
    usage "Could not determine node type. Expected (main/child)"
    ;;
esac