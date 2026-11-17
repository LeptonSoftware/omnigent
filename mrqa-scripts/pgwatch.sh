#!/bin/bash
# prints max connections per client_addr seen over the run; usage pgwatch.sh seconds outfile
end=$((SECONDS+$1)); : > $2
while [ $SECONDS -lt $end ]; do
  sudo -n docker exec mrqa-pg psql -U omnigent -d omnigent -Atc "select now()::time(0), count(*), string_agg(a||':'||n, ' ') from (select coalesce(host(client_addr),'local') a, count(*) n from pg_stat_activity where backend_type='client backend' group by 1 order by 1) s" >> $2 2>&1
  sleep 0.5
done
