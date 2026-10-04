# Run Panini Watch 24/7 without your PC

It needs a small always-on Linux server (a "VPS"). About **$4–6 / month**, or free on Oracle's
"Always Free" tier. 2 GB RAM is comfortable, 1 GB works.

## 1. Get a server (5 minutes, once)

Any of these work — pick one, choose **Ubuntu 24.04**, and note the **IP address** and **root password**
(or add your SSH key):

| Provider | Plan | ≈ price |
|---|---|---|
| Hetzner Cloud | CX22 (2 vCPU / 4 GB) | €4 / mo |
| DigitalOcean | Basic 2 GB | $12 / mo (1 GB $6) |
| Vultr / Linode | 1–2 GB | $5–10 / mo |
| Oracle Cloud | Always Free ARM VM | free (needs a card to verify) |

Pick a datacenter in **Europe** (Panini's main shops are European).

## 2. Deploy (one command, from this PC)

```
deploy_to_vps.bat root@YOUR.SERVER.IP
```

It stops the copy on your PC, uploads the project (with its database, translations and `.env`), installs Docker on
the server and starts the bot. From then on your PC can be off. Send `/status` to your bot to confirm.

Update later (e.g. after changing `config.yaml`): run the same command again.

## Useful commands (on the server)

```
cd /opt/panini-watch
docker compose logs -f          # live log
docker compose restart          # restart
docker compose down             # stop
```

## Good to know

* **Only one copy may run at a time** (Telegram allows one listener per bot). The deploy script stops the PC copy;
  do not start `run.bat` again while the server is running.
* If Panini's shops start refusing the server's IP (errors in `/status`), set a proxy in `.env`:
  `PANINI_PROXY=http://user:pass@host:port` and redeploy.
* Keep the bot token private. If it leaks: @BotFather → `/revoke`, put the new token in `.env`, redeploy.
