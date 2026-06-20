Step 1: Add the cron job
On EC2:


crontab -e
(Pick nano if prompted — option 1.)

Add this line at the bottom:


0 * * * * /opt/vfs-malta-slot-checker/run_ec2.sh >> /opt/vfs-malta-slot-checker/app.log 2>&1
Save & exit: Ctrl+O → Enter → Ctrl+X. You'll see crontab: installing new crontab.

Step 2: Confirm it's scheduled

crontab -l
You should see your line. This only confirms it's scheduled, not that it has run yet — cron fires at minute 0 of the next hour.

How to check cron's status / whether it ran
Since cron runs in the background (not in your terminal), here's how to see what it's doing:

A. Watch the live log

tail -f /opt/vfs-malta-slot-checker/app.log
At the top of the next hour you'll see a new run appear live, starting with Starting VFS slot-check run.... (Ctrl+C stops watching, not the bot.)

B. Confirm cron actually fired it (system log)

grep CRON /var/log/syslog | tail -20
This shows cron's own record of launching run_ec2.sh — proof the scheduler triggered it, separate from whether the bot succeeded.

C. Check the last run's outcome

tail -n 40 /opt/vfs-malta-slot-checker/app.log
Look for Route X succeeded / All routes done (success) or a failure alert.

Don't wait an hour to test — force a run now
Two ways to verify cron works without waiting for the top of the hour:

Option 1 — temporarily schedule it every minute:


crontab -e
Change 0 * * * * to * * * * * (every minute), save, then watch:


tail -f /opt/vfs-malta-slot-checker/app.log
Within ~60 seconds cron launches it. Once you confirm it fired, change it back to 0 * * * * (hourly) — otherwise it runs every minute.

The flock lockfile protects you here: even if a run is still going when the next minute ticks, the second is skipped — they won't stack.

Option 2 — just run the exact cron command manually (tests the script, not the scheduler):


/opt/vfs-malta-slot-checker/run_ec2.sh >> /opt/vfs-malta-slot-checker/app.log 2>&1
My recommendation for a clean check
crontab -e → add the * * * * * (every-minute) line temporarily.
tail -f app.log → watch it fire within a minute, run all 3 routes, send 3 Telegram messages.
Once you see it work end-to-end via cron → crontab -e → change back to 0 * * * * (hourly).
crontab -l → confirm the final hourly line.
That proves cron + the bot + Telegram all work together, without waiting.

Quick status question so I can tell you exactly where you stand: have you added the cron line yet, or do you want to do the every-minute test first? And paste me the output of crontab -l + the last ~20 lines of app.log and I'll tell you the exact status.