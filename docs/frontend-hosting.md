# AWS Amplify Hosting

The React frontend is a static Vite build hosted separately from the backend SAM stack. The current public application is [available here](https://production.d315aol50xxf22.amplifyapp.com/). The following instructions publish your own build without requiring a Git push.

## Build the frontend

Set ignored `frontend/.env.local` to the `ApiUrl` output of your backend stack:

```dotenv
VITE_API_BASE_URL=https://YOUR_API_ID.execute-api.eu-central-1.amazonaws.com
```

This URL is public configuration. Vite embeds it at build time; do not put credentials in any `VITE_` value. Changing an Amplify environment variable does not rebuild a ZIP that was compiled locally.

From the repository root:

```powershell
npm --prefix frontend ci
npm --prefix frontend run build
New-Item -ItemType Directory -Force .local/frontend | Out-Null
Compress-Archive -Path frontend/dist/* -DestinationPath .local/frontend/ingestra-frontend.zip -Force
```

Stop if the build fails. The ZIP must contain `index.html` at its root and `assets/` beside it, rather than a containing `dist/` directory. Upload only compiled static files, not source, environment files, credentials, dependencies, or the backend.

## Publish through Amplify

Use an authorized Console identity with Amplify Hosting permissions. The provided `ingestra-developer` backend deployment policy has no Amplify actions; do not attach broad access to work around this.

1. Open AWS Amplify in the Console and select Frankfurt (`eu-central-1`).
2. Choose **Create new app ? Deploy without Git ? Next**.
3. Enter an app name such as `ingestra-frontend` and a branch name such as `production`.
4. Select **Drag and drop**, choose `.local/frontend/ingestra-frontend.zip`, review, then choose **Save and deploy**.
5. Copy the assigned HTTPS origin. A custom domain is optional.

The current application switches views in React state rather than pathname-based routes; its existing views do not require an SPA rewrite rule.

## Configure the backend origin

Set the backend's `FrontendOrigin` parameter to the exact assigned HTTPS origin, without paths or a trailing slash. This configures both API Gateway and the application's S3 upload bucket. Update it through SAM/CloudFormation rather than editing those services independently.

Follow [the backend deployment commands](deployment.md#validate-build-and-deploy-the-backend), changing:

```powershell
$IngestraFrontendOrigin = 'https://YOUR_BRANCH.YOUR_APP_ID.amplifyapp.com'
```

Use your own assigned URL, account, artifact bucket, and execution role. A parameter-only update does not require another backend build when the existing built artifacts are current. Rebuild the boundary-enabled template whenever backend code changes.

The template supports one frontend origin. Setting the hosted origin replaces localhost as the origin allowed by API and S3 CORS. The frontend API URL and backend tenant remain unchanged.

## Verify and maintain

After the stack update, load the hosted page and verify inventory reads, job creation, direct S3 upload, preview, import, and rollback with non-sensitive sample data. The upload must pass both API POST and S3 PUT preflight checks.

For subsequent frontend releases, build and upload a new ZIP to the same Amplify app and branch. The backend stack does not own this hosting deployment; remove the Amplify app independently when it is no longer required. Hosting usage can incur charges.

Sources: [AWS manual deployment steps and ZIP layout](https://docs.aws.amazon.com/amplify/latest/userguide/manual-deploys.html), [Amplify Hosting pricing](https://aws.amazon.com/amplify/pricing/).
