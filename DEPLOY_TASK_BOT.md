# 🚀 How to Deploy the Valence Task Bot to Render (Step-by-Step)

## Introduction
The Valence Task Bot runs independently of the main study bot to handle specific task-related commands and logic. This guide outlines the steps needed to deploy the new service `valence-task-bot` to Render using the configuration defined in `render.yaml`.

---

## Step-by-Step: Deploy to Render (Free Tier)

### Step 1: Access Your Render Account
1. Go to [Render](https://render.com) and log in.
2. Ensure you have access to the connected GitHub repository: `peacedestroyer69/valence-study-bot`.

### Step 2: Create a New Web Service
1. Click **"New +"** in the top right → Select **"Web Service"**.
2. Connect your GitHub account if prompted.
3. Select the repository: **`peacedestroyer69/valence-study-bot`**.
4. Configure the service with the following settings:
   - **Name:** `valence-task-bot`
   - **Region:** Pick the region closest to your users (e.g., Singapore for Asia/India region)
   - **Branch:** `main` (or the branch where your task bot code is merged)
   - **Runtime:** `Python`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python task_bot.py`
   - **Instance Type:** **Free**

### Step 3: Add Environment Variables
In the Render dashboard under the **"Environment"** tab, add the required environment variables:

| Key | Value |
|-----|-------|
| `TASK_BOT_TOKEN` | Your Discord bot token for the task bot (starts with `MTUx...`) |

*(Note: If the task bot shares other resources or requires additional channel IDs, they can also be added here as environment variables).*

### Step 4: Deploy the Service
1. Click **"Create Web Service"** at the bottom of the page.
2. Render will trigger the initial build, install dependencies from `requirements.txt`, and boot the service using `python task_bot.py`.
3. Check the Render logs to ensure there are no startup errors and look for standard log messages indicating the bot is online.

### Step 5: Set Up UptimeRobot (Keep Service Awake)
Like all Render free-tier web services, this bot will spin down after 15 minutes of inactivity. To keep the task bot active 24/7, set up a ping:
1. Log in to [UptimeRobot](https://uptimerobot.com) (or a similar uptime monitor).
2. Click **"Add New Monitor"**.
3. Configure the monitor:
   - **Monitor Type:** `HTTP(s)`
   - **Friendly Name:** `Valence Task Bot`
   - **URL:** `https://valence-task-bot.onrender.com` (replace with your actual Render URL shown on the dashboard)
   - **Monitoring Interval:** `5 minutes`
4. Click **"Create Monitor"**.

### Step 6: Verify
1. Go to your Discord server and confirm that the new Task Bot shows as **Online** (green status dot).
2. Verify that the task commands function as expected.
3. Check the Render logs for the startup messages and command registration confirmation.

---

## Troubleshooting & Common Issues

- **Bot Status is Offline:**
  - Verify that the `TASK_BOT_TOKEN` in the Render Environment tab matches the credentials from your Discord Developer Portal exactly.
  - Review Render build and runtime logs for syntax errors, missing package dependencies, or initialization crashes.
- **Port Binding / Health Check Failures:**
  - If Render logs complain that the service is failing health checks or failing to bind to a port, ensure the task bot starts a light HTTP keep-alive server (like aiohttp/Flask on port 8080 or the port provided by the `PORT` environment variable), similar to the main bot.
