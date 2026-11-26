# Hosting Mizan on Windows Server

*The app is now called Mizan. The scheduled tasks and firewall rule keep their old names (**AG Inventory**, **AG Inventory daily backup**, **AG Inventory (TCP 5000)**) so existing setups keep working.*

This guide sets up Mizan on a Windows Server (2016, 2019, 2022 or 2025) so that it:

- starts by itself when the server starts, with nobody logged in,
- starts again by itself if it ever stops,
- can be reached by every PC, tablet and phone on the shop network,
- backs up all data every day.

It also works the same way on an always-on Windows 10 or 11 PC. There is no Docker, no database server and no IIS needed. The app runs on **Waitress**, a production web server that is already part of Mizan (`serve.py`).

Plan about 20 minutes. Each step says what you should see when it worked.

## What you need

- A Windows Server you can log in to as an **Administrator**.
- Internet on the server during setup (to download Python and two small packages). The shop can work offline afterwards.
- The server connected to the shop network (cable is best, Wi-Fi works too).

## Step 1. Give the server a fixed address

Shop devices open the app by the server's address, for example `http://192.168.1.10:5000`. If the address changes, they lose the app. Make it fixed in one of two ways:

- **Easiest:** in your Wi-Fi router's settings, find *DHCP reservation* (or *Static lease*) and reserve the current address for the server.
- **Or on the server:** *Server Manager → Local Server → Ethernet → Properties → Internet Protocol Version 4 → Use the following IP address*.

To see the current address, open **Command Prompt** and run `ipconfig`. Look for *IPv4 Address*.

## Step 2. Install Python

1. On the server, open <https://www.python.org/downloads/windows/> and download the newest **Python 3** *Windows installer (64-bit)*. Python 3.10 or newer is needed.
2. Run the installer. On the first screen:
   - tick **Add python.exe to PATH**,
   - click **Customize installation**, then **Next**, then tick **Install Python for all users**, then **Install**.

   *Install for all users* matters: the app runs as a background service account, which must be able to use Python.
3. Check it: open a **new** Command Prompt and run `python --version`. You should see `Python 3.x.x`.

## Step 3. Put Mizan on the server

Use the folder **`C:\AG_Inventory`**. A short path without spaces avoids problems.

**Option A, with Git (recommended, because updates are one command).**
Install Git for Windows from <https://git-scm.com/download/win> (the default choices are fine). Then, in a new Command Prompt:

```bat
cd C:\
git clone https://github.com/AGSoftwareplc/AG_Inventory.git AG_Inventory
```

If the repository is private, Git asks you to sign in to GitHub.

**Option B, without Git.** On GitHub, open the repository, click **Code → Download ZIP**, and unzip it so that the files (`app.py`, `serve.py`, `start.bat` and so on) are directly inside `C:\AG_Inventory`.

**Moving from a shop PC that already has data?** On the old PC, stop the app (close its window). Copy its whole **`data`** folder into `C:\AG_Inventory\data` on the server. The `data` folder holds every business, branch, user, logo and the login key. Copy `backups` too if you want the old backups. Do not run the app on the old PC afterwards, or you will have two sets of data.

## Step 4. Run the setup script

1. Click **Start**, type **PowerShell**, right-click **Windows PowerShell** and choose **Run as administrator**.
2. Run:

   ```powershell
   cd C:\AG_Inventory
   powershell -ExecutionPolicy Bypass -File server\setup-windows-server.ps1
   ```

The script does five things. You can run it again at any time; it only updates what is already there.

| What | Details |
|---|---|
| Installs the app | Creates `.venv` and installs Flask and Waitress (the same as `install.bat`). |
| Creates the task **AG Inventory** | Task Scheduler starts `server\run-server.bat` at every boot as the *SYSTEM* account, so nobody needs to log in. If the app stops, it starts again after 10 seconds. |
| Creates the task **AG Inventory daily backup** | Every day at 23:00 it copies all data to `backups\server`. |
| Opens the firewall | Allows TCP port 5000 for *Domain* and *Private* networks. |
| Starts the app | And shows the address for shop devices. |

When it worked you see:

```text
Mizan is running.
  On this server:      http://localhost:5000
  From shop devices:   http://192.168.1.10:5000
It starts by itself whenever the server starts. Log: logs\server.log
```

Options, added to the end of the command:

| Option | Use it to |
|---|---|
| `-Port 8080` | use another port than 5000 |
| `-BackupTime 21:30` | run the daily backup at another time |
| `-BackupCopyTo E:\AG_Backups` | also keep every backup on a second disk or USB drive |
| `-NoFirewall` | not open the port (for example when you use IIS and HTTPS, see Step 10) |

**Network type.** The firewall rule covers *Private* and *Domain* networks, not *Public* ones. If shop devices can't connect, check the network type in *Settings → Network & Internet → Ethernet* and set it to **Private**.

## Step 5. Test from a shop device

1. On a shop PC, tablet or phone on the same network, open a browser and go to the address the script showed, for example `http://192.168.1.10:5000`.
2. The Mizan login page appears.
3. Bookmark it, or on a phone or tablet use *Add to Home screen*.

On the server itself, use <http://localhost:5000>.

## Step 6. First login

If this is a new installation (no `data` folder copied in Step 3), log in with `admin` / `admin123` and **change all starter passwords** at once (see the [README](../README.md#start)). Then open **⚙ Admin → Settings** and enter the shop name, address and phone.

## Step 7. Backups and restore

There are two kinds of backup. Both run on their own.

- **The app's own backup:** once a day, the first time someone uses the app, it copies the database of the business in use to `backups\` and keeps 30 copies. **Admin → Settings** also has *Make and download a backup now*, and *Restore*.
- **The server backup (from the setup script):** every day at 23:00, `server\backup_all.py` copies **everything** in `data\` (all businesses, `platform.db`, logos and the login key) into a new folder such as `backups\server\20261125-230000`. It keeps the newest 30. It is safe while the app is running.

Make a server backup now by hand:

```powershell
cd C:\AG_Inventory
.venv\Scripts\python server\backup_all.py
```

**Keep a copy away from the server.** A backup on the same disk doesn't help if the disk fails or the server is stolen. Use `-BackupCopyTo` (Step 4) with a second disk or USB drive, or copy the `backups\server` folder somewhere else every week. A network share works too, but the backup task runs as the server's computer account, so that account needs write access to the share.

**Restore everything from a server backup:**

1. Stop the app (PowerShell as Administrator):
   ```powershell
   cd C:\AG_Inventory
   powershell -ExecutionPolicy Bypass -File server\stop-server.ps1
   ```
2. Rename `C:\AG_Inventory\data` to `data-old`. Don't delete it until the restore is checked.
3. Copy the backup folder you want, for example `backups\server\20261125-230000`, to `C:\AG_Inventory` and rename it to `data`.
4. Start the app: `Start-ScheduledTask -TaskName 'AG Inventory'`.

To restore only one business, use **Admin → Settings → Restore** in the app while logged in to that business.

## Step 8. Update to a new version

**With Git (Option A).** In PowerShell as Administrator:

```powershell
cd C:\AG_Inventory
powershell -ExecutionPolicy Bypass -File server\update-windows-server.ps1
```

It backs up all data, stops the app, gets the new version with `git pull`, updates the packages and starts the app again. The database is upgraded automatically on start. Your `data` and `backups` folders are never touched by the update.

**Without Git (Option B).** Stop the app with `server\stop-server.ps1`. Download the new ZIP and copy its files over `C:\AG_Inventory`, but **keep the `data` and `backups` folders**. Then run:

```powershell
powershell -ExecutionPolicy Bypass -File server\update-windows-server.ps1 -NoGit
```

Tell users to reload the page (F5) after an update.

## Step 9. Everyday commands

All in PowerShell as Administrator, in `C:\AG_Inventory`.

| To | Run |
|---|---|
| Check that it runs | `Get-ScheduledTask -TaskName 'AG Inventory'` (State **Running**), or open <http://localhost:5000> |
| Stop | `powershell -ExecutionPolicy Bypass -File server\stop-server.ps1` |
| Start | `Start-ScheduledTask -TaskName 'AG Inventory'` |
| Restart | stop, then start |
| See the log | `Get-Content logs\server.log -Tail 50` |
| Back up now | `.venv\Scripts\python server\backup_all.py` |

You can also use **Task Scheduler** (Start → *Task Scheduler* → *Task Scheduler Library*): right-click **AG Inventory** and choose *Run* or *End*.

**Don't also double-click `start.bat` on the server.** The app already runs in the background. A second copy only gets in the way. `stop-server.ps1` ends every copy, including ones started with `start.bat`.

## Step 10 (optional). HTTPS and a name with IIS

You don't need this inside one shop: `http://192.168.1.10:5000` on the shop network is fine. Add HTTPS when the app is reached over a network you don't control, or when you want an address such as `https://inventory.myshop.com`.

**Never open port 5000 to the internet directly.** For branches in other places, the simplest safe choice is a VPN (for example Tailscale, free for small teams) between the branches and the server, with no IIS needed. Use IIS with HTTPS if the app must be reachable as a public website.

IIS then sits in front of the app: browsers talk HTTPS to IIS, and IIS passes each request to the app on `localhost:5000`.

1. **Install IIS:** *Server Manager → Add roles and features → Web Server (IIS)*, with the default choices.
2. **Install two free IIS add-ons** from Microsoft: **URL Rewrite** (<https://www.iis.net/downloads/microsoft/url-rewrite>) and **Application Request Routing 3.0** (<https://www.iis.net/downloads/microsoft/application-request-routing>).
3. **Turn on the proxy:** open *IIS Manager*, click the server name, open **Application Request Routing Cache → Server Proxy Settings** (right side), tick **Enable proxy**, click **Apply**. Then, in PowerShell as Administrator, keep the address the browser used:
   ```powershell
   & "$env:windir\system32\inetsrv\appcmd.exe" set config -section:system.webServer/proxy /preserveHostHeader:"True" /commit:apphost
   ```
4. **Create the site:** make a folder `C:\inetpub\aginventory`. In IIS Manager, *Sites → Add Website*: name `Mizan`, physical path `C:\inetpub\aginventory`, binding **https**, port 443, your host name, and an SSL certificate (see below). Stop or remove the *Default Web Site* if you don't use it.
5. **Pass requests to the app:** save this as `C:\inetpub\aginventory\web.config`:
   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <configuration>
     <system.webServer>
       <rewrite>
         <rules>
           <rule name="AG Inventory" stopProcessing="true">
             <match url="(.*)" />
             <action type="Rewrite" url="http://localhost:5000/{R:1}" />
           </rule>
         </rules>
       </rewrite>
       <security>
         <requestFiltering>
           <!-- allow backup restores and Excel imports up to 200 MB (IIS allows 30 MB by default) -->
           <requestLimits maxAllowedContentLength="209715200" />
         </requestFiltering>
       </security>
     </system.webServer>
   </configuration>
   ```
6. **Firewall:** open 443 and close 5000 from outside, so everyone goes through HTTPS:
   ```powershell
   New-NetFirewallRule -DisplayName 'AG Inventory HTTPS' -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
   Get-NetFirewallRule -DisplayName 'AG Inventory (TCP 5000)' | Remove-NetFirewallRule
   ```
   When you run the setup script again later, add `-NoFirewall` so it doesn't open 5000 again.

**The SSL certificate:**

- **Public name** (for example `inventory.myshop.com` pointing to the server, with port 443 forwarded on the router): get a free Let's Encrypt certificate with **win-acme** (<https://www.win-acme.com/>). It sets up the IIS binding and renews the certificate by itself.
- **Only inside the shop network:** make a self-signed certificate:
  ```powershell
  New-SelfSignedCertificate -DnsName 'aginventory.local' -CertStoreLocation Cert:\LocalMachine\My
  ```
  Choose it in the IIS binding. Browsers show a warning until the certificate is trusted on each device, which is why plain `http://` on the shop network is usually simpler.

## Troubleshooting

| Problem | What to do |
|---|---|
| The setup script says *Python 3.10 or newer was not found* | Install Python as in Step 2, then open a **new** PowerShell window (so it sees the new PATH) and run the script again. |
| *running scripts is disabled on this system* | Start the script with `powershell -ExecutionPolicy Bypass -File ...` exactly as shown. |
| It works on the server but not from shop devices | Check the address with `ipconfig`. Make sure the device is on the same network. Set the server's network to *Private* (Step 4). Check that the rule **AG Inventory (TCP 5000)** exists in *Windows Defender Firewall → Inbound Rules*. |
| Nothing opens even on the server | Read `logs\server.log`. A line with *address already in use* means another program uses the port: stop extra copies with `stop-server.ps1`, or set up with `-Port 8080`. |
| `git pull` says *dubious ownership* | Run `git config --global --add safe.directory C:/AG_Inventory` once, then update again. |
| The address changed after a power cut | Reserve the address in the router (Step 1). |
| Users are logged out after moving to the server | Copy `data\secret.key` from the old PC too, or simply log in again. |

## Remove it

In PowerShell as Administrator:

```powershell
cd C:\AG_Inventory
powershell -ExecutionPolicy Bypass -File server\stop-server.ps1
Unregister-ScheduledTask -TaskName 'AG Inventory' -Confirm:$false
Unregister-ScheduledTask -TaskName 'AG Inventory daily backup' -Confirm:$false
Get-NetFirewallRule -DisplayName 'AG Inventory (TCP 5000)' | Remove-NetFirewallRule
```

Keep the `data` and `backups` folders: they hold all your business data.

## Files used on the server

| File | What it does |
|---|---|
| `server\setup-windows-server.ps1` | One-time setup (Step 4). Safe to run again. |
| `server\run-server.bat` | What the *AG Inventory* task runs: starts `serve.py` with no browser window, writes `logs\server.log`, restarts it if it stops. |
| `server\stop-server.ps1` | Stops the task and every running copy of the app. |
| `server\update-windows-server.ps1` | Backup, stop, update, start (Step 8). |
| `server\backup_all.py` | Backs up all of `data\` safely while the app runs (Step 7). |
