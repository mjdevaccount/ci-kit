# alloc.jq -- the shared slot budget's ONE decision, as a pure function (no I/O): the supervisor
# on a runner VM gathers the inputs, runs `jq -c -f alloc.jq`, and only acts on the verdict. Its
# Python twin is ci_kit.runners.alloc.allocate; tests/test_runners_alloc_parity.py pins the two
# verdicts equal on a matrix of inputs. Ships with ci-kit (MIT); a kit's `alloc_jq` copies it.
#
# In:  {max_total, mem_budget_mb, [mem_base_mb, live_free_mb], peaks: {repo: peak_mb}, prev_head, pools: [{repo, min, max, running, busy, idle, queued, oldest}]}
#        prev_head = the head the LAST run drained for (optional; the supervisor keeps it in /run)
#        running = live runner containers (registering ones included: they will take a job)
#        busy    = runners GitHub reports busy;  idle = online and not busy (the only reclaimable)
#        queued  = this repo's QUEUED jobs that want this pool;  oldest = its oldest queued run (s)
#        since_start = s since this pool last got a runner (optional, 0 = never ages: AGE_S)
#        blind   = the tick could not read this pool (optional; a blind carried head keeps its room)
#        backfill = false turns the drain BACKFILL off (optional; default on; the block's `backfill`)
#        peaks   = each pool's MEASURED one-job peak (the render's peak_mb); a pool with none
#                  (a runner of a repo no longer in the config) is charged the largest declared
#        starve_s = per-pool STARVE_S override (optional; the render's starve_s), {repo: s}
# Out: {free, free_mb, drain, head, committed_mb, pools: [{repo, desired, start, reclaim, held, cap}]}
#        cap = the most this pool may be REFILLED to this run (the supervisor's TARGET is
#              min(running + start, desired, cap)); below desired only while draining
#
# STARVATION. Reserving a held pool's peak the moment it does not fit, against only the pools
# LATER in the order and with nothing ever giving the room back, let one big pool hold every
# smaller pool after it at zero runners while it waited on room that never freed. So: a pool that
# does not fit is held WITHOUT a reservation, and smaller pools backfill the room it cannot use,
# until its oldest job has waited STARVE_S. Then it is STARVED and picks first, FEWEST RUNNERS
# first, so a pool with a deep backlog cannot take every freed slot from pools that have none. A
# starved pool that fits (memory beside the reservations above it, and a slot beside the reserved
# ones) starts one. One that does not -- out of memory OR out of slots -- RESERVES its peak and a
# slot if it could ever fit, and the run DRAINS: no ROUND-ROBIN starts (floors and starved pools
# that fit beside the reservations still start), and every pool but the HEAD (the first to
# reserve) has its refill cap dropped to its floor, so runners are not replaced as they finish and
# the room comes back. "Could ever fit" = every other pool at its floor, plus this one at max(its
# live runners, its floor), plus the one it waits for, at peaks <= mem_budget_mb and in runners
# <= max_total. A pool that never can is only held (watch held pools from outside). And only a
# pool with NO runner reserves (FIRST RUNNER ONLY): a pool already holding a busy runner that led
# a drain for its second would hold every other pool at its floor while the VM had room.
# The head is CARRIED across runs (prev_head) and, while it is still starved, picks first: the
# reservation only binds pools after it in the pass, so a deep-backlog pool drained to zero
# runners would otherwise sort ahead of it the next run and take back the very room just freed.
# The pass ranks on runners AFTER the floors' starts, so a pool topped up to its floor this run is
# not "fewest runners".

# desired = min(max, max(min, busy + queued)): the floor when quiet, one runner per job in a burst.
# Slots go first to pools below their floor, then one at a time, round-robin, to pools with unmet
# demand, the oldest queue first -- never past max_total, and never past mem_budget_mb counted at
# each live runner's pool peak (MEMORY-WEIGHTED ADMISSION: a count cap has to assume every slot is
# the hungriest pool). A pool whose next runner does not fit is HELD (STARVATION above says when
# that turns into a reservation). Demand left over takes IDLE runners back from pools above their
# desired count; their slots free up when they are gone, so the starts come the next tick. Busy
# runners are never taken: an ephemeral runner leaves on its own after its job.

def clamp0: if . < 0 then 0 else . end;
# a held pool's oldest job waits this long (s) before the run drains the others for it
def STARVE_S: 300;
# PER-POOL STARVE_S. The no-reservation window exists so smaller pools backfill room a held big
# pool cannot use YET. A pool whose peak needs the VM all but EMPTY is drained for on every start
# anyway -- nothing else ever leaves it that room -- so for it the window only lets the round-robin
# start runners the drain then has to wait out. Its `starve_s` gives it its own threshold (0 = it
# reserves the first pass it does not fit). Fairness is unchanged: the head carry still ends at its
# FIRST runner, and AGING still ranks the longest-unserved pool ahead of it.
# AGING: a starved pool UNSERVED this long (s) -- no runner started for it, while its oldest job
# waited at least as long -- picks ahead of the fewest-runners order, longest unserved first. Only a
# pool at ZERO runners ages (FIRST RUNNER ONLY): since_start is the last START, so a pool whose one
# runner is running a long job reads "unserved" while it is being served.
def AGE_S: 1200;

.max_total as $total
| (.mem_budget_mb // error("mem_budget_mb missing: count-only admission is refused")) as $budget
| (.peaks // error("peaks missing")) as $peaks
| (.prev_head // null) as $prev
| (.starve_s // {}) as $starve
| (.live_free_mb // null) as $live
| (.mem_base_mb // null) as $base
# COULD IT EVER FIT is a question about the REAL RAM: with the booking past it (mem_book_ratio),
# a drain for a runner that only fits the booking holds every pool at its floor for room the
# live cap may never give
| ($base // $budget) as $fit
# backfill false = the drain without BACKFILL: the tests' no-backfill baseline (a start the
# backfill run has beyond it IS a backfill) and a kill switch; absent = on
| (.backfill != false) as $bfon
| ([$peaks[]] | max // 0) as $maxpeak
| if (.pools | length) > 0 and $maxpeak <= 0 then error("no positive peak: admission would be count-only") else . end
| [ .pools[]
    | . + {desired: ([.max, ([.min, (.busy + .queued)] | max)] | min), peak: ($peaks[.repo] // $maxpeak)}
    | . + {need: ((.desired - .running) | clamp0), floor_need: ((.min - .running) | clamp0), start: 0, held: false} ]
  # oldest queue first; repo name breaks ties so the order (and so the test) is stable
| sort_by([-(.oldest), .repo])
# LIVE CAP: $budget may be booked past the RAM (mem_book_ratio); the VM's real MemAvailable less
# live_floor_mb then caps the room this run can hand out. A failed live read (live_free_mb null)
# falls back to the unscaled base: never an overcommit admitted blind.
| (map(.running * .peak) | add // 0) as $booked
| ([$budget - $booked,
    (if $live != null then $live elif $base != null then $base - $booked else $budget - $booked end)]
   | min) as $room
| { free: (($total - (map(.running) | add // 0)) | clamp0),
    free_mb: $room,
    pools: . }

# floors first (`ci-runners --check` proves the floors fit the budget at their peaks)
| reduce range(0; .pools | length) as $i (.;
    ([.pools[$i].floor_need, .free, (if .pools[$i].peak > 0 then (.free_mb / .pools[$i].peak | floor | clamp0) else .free end)] | min) as $g
    | .pools[$i].start += $g | .pools[$i].need -= $g | .free -= $g | .free_mb -= ($g * .pools[$i].peak))

# STARVED FIRST (see the header): every pool whose oldest job has waited STARVE_S gets the first
# pick of what the floors left, FEWEST RUNNERS first (then oldest), one runner each. A starved
# pool that does not fit -- memory, or no slot (`free` 0: the slot cap binds and nothing would
# ever give one back) -- RESERVES its peak and one slot when it could ever fit, and pools after
# it in this pass see only what is left beyond the reservations.
| ([.pools[] | .min * .peak] | add // 0) as $floor_mb
| ([.pools[] | .min] | add // 0) as $floor_n
| .reserved = 0 | .rslots = 0 | .head = null
# A carried head the supervisor could not READ this tick (blind: booked queued 0) still owns the
# room: it reserves its peak and a slot first, exactly as if it had read starved. Without this
# one API hiccup would end the drain and the room would go to whoever sorted first.
| (.pools | map(select(.repo == $prev and (.blind // false))) | first // null) as $bh
# a blind head that already holds a runner is served: it sets nothing aside (FIRST RUNNER ONLY)
| if $bh != null and ($bh.running == 0)
     and ($floor_mb - $bh.min * $bh.peak + ([$bh.running, $bh.min] | max) * $bh.peak + $bh.peak) <= $fit
     and ($floor_n - $bh.min + ([$bh.running, $bh.min] | max) + 1) <= $total
  # it FITS now: set its room aside (no start: the tick cannot see it) and do NOT drain -- the
  # others round-robin in what is left. It does not fit: reserve and drain for it, as a readable
  # head would.
  then (if .free > 0 and $bh.peak <= .free_mb then .free -= 1 | .free_mb -= $bh.peak
        else .reserved += $bh.peak | .rslots += 1 | .head = $prev end)
  else . end
# The pass: starved pools, plus the carried head while it still has a queue -- STARVE_S is the
# bar to BECOME the head, not to stay it: a head whose oldest job reads young this tick (a partial
# jobs read, a rerun's fresh job) is still waiting on the room drained for it.
# Order: the carried head; then AGED pools, longest unserved first; then fewest runners, oldest
# queue. Unserved = min(since_start, oldest): seconds since this pool last got a runner (the
# supervisor's record; absent = 0, never aged), but never more than its oldest job has waited.
# Without it the same pool could win every drain: drained to zero runners, it re-ranks "fewest
# runners, oldest queue" the moment its job ends and takes the room left after each head's start.
# The carry ends once the head is SERVED (a live runner, or one this run's floors started): it
# exists to land a head's first runner, not to give a served head first pick of its next one.
| (.pools | map(select(.repo == $prev and (.running + .start) == 0)) | first | .repo // null) as $carry
| reduce (.pools | to_entries
          | map(select(.value.need > 0 and (.value.oldest >= ($starve[.value.repo] // STARVE_S) or .value.repo == $carry)))
          | map(. + {u: ([(.value.since_start // 0), .value.oldest] | min)})
          | sort_by([(if .value.repo == $carry then 0 else 1 end),
                     (if .u >= AGE_S and (.value.running + .value.start) == 0 then -(.u) else 0 end),
                     .value.running + .value.start, -(.value.oldest), .value.repo]) | .[].key) as $i (.;
    .pools[$i] as $p
    | if (.free - .rslots) > 0 and $p.peak <= (.free_mb - .reserved)
      then .pools[$i].start += 1 | .pools[$i].need -= 1 | .free -= 1 | .free_mb -= $p.peak
      # could it EVER fit: every other pool at its floor, this one at its own live count (the
      # head keeps its refill, so its runners stay) plus the one it waits for
      # FIRST RUNNER ONLY: a pool that already holds a runner is being served; draining the VM for
      # its NEXT one would hold every other pool at its floor. It still starts one when room is
      # free (the branch above, or the round-robin); it is only never the reason to drain.
      elif ($p.running + $p.start) == 0
           and ($floor_mb - $p.min * $p.peak + ([$p.running, $p.min] | max) * $p.peak + $p.peak) <= $fit
           and ($floor_n - $p.min + ([$p.running, $p.min] | max) + 1) <= $total
      then .pools[$i].held = true | .reserved += $p.peak | .rslots += 1 | .head //= $p.repo
      else .pools[$i].held = true end)
| .drain = (.head != null)

# then the shared slots, one at a time across the pools still short; a pool that does not fit is
# held (no reservation: smaller pools backfill the room). Draining: only the BACKFILL below.
#
# BIGP-BOUNDED BACKFILL (after Lifka 1995's EASY backfill, which Slurm's sched/backfill keeps --
# but NOT EASY's guarantee, see the trade-off). A drain that holds EVERY pool for the head leaves
# room idle that smaller pools could use. We cannot know when a runner ends, so the guarantee is
# memory-only: the head starts no later than the end of BIGP, the LARGEST runner that gives room
# back (a non-head pool's runner above its floor: draining caps it at its floor, so it is not
# replaced; the head has none -- only a pool at zero runners leads a drain). Backfill may spend
# A = min(free_mb, free_mb + BIGP - head.peak) - (what floors still short will take first): when
# BIGP ends, what is left is >= head.peak. Slots need no allowance: BIGP's end frees one.
# Round-robin, one runner per pool per round, in the starved pass's order (aged, fewest runners,
# oldest queue), so a deep backlog cannot take the whole allowance; held pools are candidates too.
# A backfilled runner is a START, never a refill (cap stays at the floor): when it ends its room
# goes back to the drain.
# TRADE-OFF: BIGP is the MAX, so the head may start LATER than it would with no backfill -- a
# smaller runner's end, which alone would have fit the head, can go to a backfill instead, and the
# head waits up to BIGP's remaining runtime. EASY proper takes the MIN (any one ending fits the
# head): that never delays the head, but gives back almost none of the idle room. Watch a head
# left waiting from outside (held-pool and job-wait alerts). Kill switch: the block's
# `backfill: false`.
# BIGP 0 (nothing gives room back) means A < 0 and nothing backfills -- the plain drain.
| if .drain and ($bfon | not) then .   # draining without backfill: no round-robin starts
  elif .drain then
    .head as $h
    | (.pools | map(select(.repo == $h)) | first // null) as $hp
    | if $hp == null then . else
        ([.pools[] | if .repo != $h and .running > .min then .peak else 0 end] | max // 0) as $bigp
        # a floor still short after this run's starts takes freed room FIRST next tick (the floors
        # step runs before the starved pass), so the backfill leaves its room and slots too
        | ([.pools[] | ((.min - .running - .start) | clamp0) * .peak] | add // 0) as $fl_mb
        | ([.pools[] | (.min - .running - .start) | clamp0] | add // 0) as $fl_n
        | .bf = ([.free_mb, (.free_mb + $bigp - $hp.peak)] | min) - $fl_mb
        | (.pools | to_entries
           | map(select(.value.repo != $h and .value.need > 0))
           | map(. + {u: ([(.value.since_start // 0), .value.oldest] | min)})
           | sort_by([(if .u >= AGE_S and (.value.running + .value.start) == 0 then -(.u) else 0 end),
                      .value.running + .value.start, -(.value.oldest), .value.repo])
           | map(.key)) as $order
        | .bfstop = []
        | until(.free <= $fl_n or ([$order[] as $i | select((.bfstop | index($i)) == null)] | length) == 0;
            reduce $order[] as $i (.;
              if (.bfstop | index($i)) != null then .
              elif .free > $fl_n and .pools[$i].need > 0 and .pools[$i].peak <= .bf
              then .pools[$i].start += 1 | .pools[$i].need -= 1 | .pools[$i].held = false
                   | .free -= 1 | .free_mb -= .pools[$i].peak | .bf -= .pools[$i].peak
              # a pool that stops fitting in MEMORY with work left is HELD, as in the round-robin;
              # out of SLOTS is not held (that would blame memory for a slot cap)
              else .bfstop += [$i]
                   | (if .pools[$i].need > 0 and .free > $fl_n then .pools[$i].held = true else . end) end))
        | del(.bf, .bfstop)
      end
  else
    until(.free <= 0 or ([.pools[] | select(.need > 0 and (.held | not))] | length) == 0;
      reduce range(0; .pools | length) as $i (.;
        if .free > 0 and .pools[$i].need > 0 and (.pools[$i].held | not) then
          if .pools[$i].peak <= .free_mb
          then .pools[$i].start += 1 | .pools[$i].need -= 1 | .free -= 1 | .free_mb -= .pools[$i].peak
          else .pools[$i].held = true end
        else . end))
  end
# draining, ONLY the head (the first pool that reserved) keeps its refill: a second reserving pool
# kept refilled would hold the very room the head waits for
| .head as $head
| .pools |= map(.cap = (if $head != null and .repo != $head then .min else .desired end))

# what is still short takes back idle surplus, largest surplus first (above cap: a drain hands
# idle runners back now instead of waiting for them to take a job)
| ([.pools[].need] | add // 0) as $unmet
# Draining (cap below desired), two runners are not surplus: any of a pool granted a start this
# run -- its idle one counted as surplus beside the one it was just granted -- and an idle one its
# OWN queue is about to use: taking it frees no room, it only loses that job a runner
| .pools |= map(. + {surplus: (
    if .cap >= .desired then ([(.running - .cap), .idle] | min | clamp0)
    elif .start > 0 then 0
    else ([(.running - .cap), (.idle - .queued)] | min | clamp0) end)})
| .pools |= (sort_by([-(.surplus), .repo])
    | reduce range(0; length) as $i ({left: $unmet, out: .};
        ([.out[$i].surplus, .left] | min) as $r
        | .out[$i].reclaim = $r | .left -= $r)
    | .out)
# committed_mb: what this run holds + grants at peaks (the supervisor's committed gauge). Not
# budget - free_mb: under the LIVE CAP free_mb is the live room, and the difference would read a
# tight VM as an overcommitted booking.
| { free, free_mb, drain, head,
    committed_mb: ([.pools[] | (.running + .start) * .peak] | add // 0),
    pools: (.pools | sort_by(.repo) | map({repo, desired, start, reclaim, held, cap})) }
