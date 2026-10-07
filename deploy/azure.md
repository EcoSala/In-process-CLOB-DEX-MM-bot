# Azure VM — paper MM + recording (no real orders)

This is a **paper** bot. It never sends orders. `python run.py` is the server entrypoint.

Default markets: BTC-USD, ETH-USD, SOL-USD, ARB-USD, FIL-USD.

## VM size (fits ~$100 / 3 months)

| Piece | Suggestion |
|---|---|
| Image | Ubuntu 22.04 LTS |
| Size | `Standard_B2s` (2 vCPU, 4 GB) |
| Region | West Europe or East US — pick whichever pings `api.starknet.extended.exchange` lower |
| OS disk | 30 GB is enough for code + logs |
| **Data disk** | **64–128 GB** Standard SSD, mount at `/data` |

**If you delete the VM and its OS disk, recordings on the OS disk are gone.** Put L2/trades/quotes/fills/metrics on the **data disk**.

## 1. Disk

After attaching a data disk in the Azure portal:

```bash
# find the new disk (often /dev/sdc)
lsblk
sudo mkfs.ext4 -L mmdata /dev/sdc   # only if the disk is empty
sudo mkdir -p /data
echo 'LABEL=mmdata /data ext4 defaults,nofail 0 2' | sudo tee -a /etc/fstab
sudo mount -a
sudo mkdir -p /data/mm_bot/recordings
sudo chown -R "$USER:$USER" /data/mm_bot
```

## 2. Code + venv

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip
git clone <YOUR_REPO_URL> ~/mm_bot    # or scp -r the repo
cd ~/mm_bot
bash deploy/install.sh
```

In `config.yaml`:

```yaml
recording:
  dir: "/data/mm_bot/recordings"
```

Leave `markets_mode: static` and the five names. Do not set `all`.

## 3. systemd

```bash
cd ~/mm_bot
sed -i "s/REPLACE_USER/$USER/g" deploy/mm-bot.service
sudo cp deploy/mm-bot.service /etc/systemd/system/mm-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now mm-bot
```

Linger is only needed for **user** systemd (`systemctl --user`). This unit is **system** (`/etc/systemd/system`), so linger is optional.

```bash
journalctl -u mm-bot -f
tail -f ~/mm_bot/logs/mm.log
```

Stop (flushes gzip recorder, ~a few seconds):

```bash
sudo systemctl stop mm-bot
```

## 4. Where data lives

| Stream | Path |
|---|---|
| L2 book | `{recording.dir}/{market}/book/YYYYMMDD_HH.jsonl.gz` |
| Public trades | `{recording.dir}/{market}/trades/...` |
| Paper quotes | `{recording.dir}/{market}/quotes/...` |
| Paper fills | `{recording.dir}/{market}/fills/...` |
| PnL / metrics | `{recording.dir}/metrics/YYYYMMDD_HH.jsonl.gz` |
| Text log | `~/mm_bot/logs/mm.log` (rotated) |
| Fill log | `~/mm_bot/logs/fills.log` (append + rotate; **not** truncated on restart) |

Peek last metrics line:

```bash
python3 -c "import gzip,glob,os; p=sorted(glob.glob('/data/mm_bot/recordings/metrics/*.jsonl.gz'))[-1]; print(gzip.open(p,'rt').read().strip().splitlines()[-1])"
```

## 5. Optional Blob backup

Hourly gzips are already rotated. Copy completed hours off the VM so a disk failure is not fatal:

```bash
# install azcopy, then e.g. a cron hourly:
azcopy copy "/data/mm_bot/recordings" "https://<account>.blob.core.windows.net/<container>/recordings" --recursive
```

Or `rsync` to another box. Do **not** copy the hour file still being written if you can avoid it; copy `YYYYMMDD_HH` after that hour has closed.

## 6. Do not

- Run `python main.py` on the VM (PySide6 / display).
- Set `extended.markets_mode: all` (dozens of websockets).
- Keep 3 months of L2 on the **OS** disk.
- Expect the desktop UI to show 3 months of PnL — use `metrics/` jsonl + `journalctl`.
