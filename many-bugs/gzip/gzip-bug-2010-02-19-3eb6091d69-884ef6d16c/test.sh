#!/bin/bash
EXECUTABLE=$( dirname $1 )
TEST_ID=$2
DIR=$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )

#Check if coverage is being run. If so, don't use time limit.
if [ `basename $1` = "coverage" ] ; then
  TIMEOUT=120
else
  TIMEOUT=60
fi

run_test()
{
    pushd gzip
    timeout $TIMEOUT $DIR/test.pl $1
    RESULT=$?
    popd
    return $RESULT
}

case $TEST_ID in
    p3) run_test 4 && exit 0 ;; 
    p4) run_test 5 && exit 0 ;; 
    p5) run_test 7 && exit 0 ;; 
    p6) run_test 8 && exit 0 ;; 
    p7) run_test 9 && exit 0 ;; 
    p8) run_test 12 && exit 0 ;; 
    n1) run_test 6 && exit 0 ;; 
esac
exit 1