# Google Ads MCP Server

This repo contains the source code for running an
[MCP](https://modelcontextprotocol.io) server that interacts with the
[Google Ads API](https://developers.google.com/google-ads/api).

The read-only reporting tools are described below. The write (mutate) tools
added by this project — campaign, ad group, ad, asset, extension and targeting
management, all with a dry-run safety default — are documented in
[README-EXTENDED.md](README-EXTENDED.md).

## Tools

The server uses the
[Google Ads API](https://developers.google.com/google-ads/api/reference/rpc/latest/overview)
to provide several
[Tools](https://modelcontextprotocol.io/docs/concepts/tools) and [Resources](https://modelcontextprotocol.io/docs/concepts/resources) for use with LLMs and AI agents.

### Tools available

- `search`: Retrieves information about the Google Ads account.
- `get_resource_metadata`: Retrieves metadata about a Google Ads API resource type, for example "campaign". This is useful to understand the structure of the data and what fields are available for querying.
- `list_accessible_customers`: Returns ids of customers directly accessible
  by the user authenticating the call.

### Configuring and Namespacing Tools

The Google Ads MCP server uses the `tools_config.yaml` to let you selectively enable or disable individual tools or tool categories (namespaces) and customize their namespace prefixes.

A default `tools_config.yaml` with all tools enabled is bundled with the package, so the server works out of the box with no extra setup. To customize your installation, the server resolves the configuration in the following order:

1. An explicit path set via the `GOOGLE_ADS_MCP_TOOLS_CONFIG` environment variable.
2. A `tools_config.yaml` file in the current working directory.
3. The default `tools_config.yaml` bundled with the package.

If an explicitly requested configuration file (via the environment variable) is missing, or any resolved file is invalid, the server raises an error and fails to start.

#### Configuration Example:
```yaml
namespaces:
  # Option 1: Enable category 'customers' with default prefix -> "customers_list_accessible_customers"
  customers: true

  # Option 2: Enable category 'search' with a custom prefix -> "query_search"
  search: "query"

  # Option 3: Fine-grained control over tools in a category
  metadata:
    enabled: true
    prefix: "metadata"
    enabled_tools:
      - get_resource_metadata: true
```

#### Opt-in profiles

The default configuration enables all 16 namespaces (98 tools). If you do not
need the full surface — for reporting-only use, or to reduce the tool schemas
sent to your LLM on every request — save one of the profiles below to a file
and point `GOOGLE_ADS_MCP_TOOLS_CONFIG` at it.

Note the three-state semantics of the `namespaces` key: **omitting the key
entirely keeps the enable-all default**, while a `namespaces:` block that is
present but empty (no children, or `{}`) enables **nothing**. To restrict the
server, list exactly the namespaces you want.

Read-only (reporting and metadata, no writes):

```yaml
namespaces:
  customers: true
  search: true
  metadata: true
```

Read + core mutate (adds Search campaign/ad group/keyword/ad management):

```yaml
namespaces:
  customers: true
  search: true
  metadata: true
  mutate: true
```

Full (all 16 namespaces — identical to the bundled default):

```yaml
namespaces:
  customers: true
  search: true
  metadata: true
  mutate: true
  demandgen: true
  pmax: true
  extensions: true
  targeting: true
  shopping: true
  video: true
  display: true
  tracking: true
  audiences: true
  optimize: true
  negatives: true
  experiments: true
```


### Resources available

- `discovery-document`: Retrieve the Google Ads API discovery document. Provides the discovery document for the latest version of the Google Ads API, which describes the API surface, including resources, methods, and schemas. Host LLMs should access this resource to understand the structure of the Google Ads API and discover available features.
- `metrics`: Retrieve information about the metrics available for reporting in the Google Ads API.
- `segments`: Retrieve information about the segments available for reporting in the Google Ads API.
- `release-notes`: Retrieve the release notes for the latest version of the Google Ads API.

## Notes

1.  The MCP Server will expose your data to the Agent or LLM that you connect to it.
1.  If you have technical issues, please use the issue tracker of this
    repository.
1.  The server appends a `google-ads-mcp/<version>` token to the
    `x-goog-api-client` header of every Google Ads API request, so calls
    made through this server are identifiable in API logs.

## Setup instructions

Setup involves the following steps:

1.  Configure Python.
1.  Configure Developer Token.
1.  Enable APIs in your project
1.  Configure Credentials.
1.  Configure your MCP client.

### Configure Python

[Install pipx](https://pipx.pypa.io/stable/#install-pipx).

### Configure Developer Token

Follow the instructions for [Obtaining a Developer Token](https://developers.google.com/google-ads/api/docs/get-started/dev-token).

Your developer token must have at least [Explorer access](https://developers.google.com/google-ads/api/docs/get-started/dev-token#access-levels) to query production accounts. New tokens may be automatically upgraded to Explorer access; if not, you can apply through the API Center. See the [access levels documentation](https://developers.google.com/google-ads/api/docs/get-started/dev-token#access-levels) for details.

If you see the error *"The developer token is only approved for use with test
accounts"*, your token does not yet have access to production accounts. See the
[access levels documentation](https://developers.google.com/google-ads/api/docs/access-levels)
for how to request the access level you need.

### Enable APIs in your project

[Follow the instructions](https://support.google.com/googleapi/answer/6158841)
to enable the following APIs in your Google Cloud project:

* [Google Ads API](https://console.cloud.google.com/apis/library/googleads.googleapis.com)

### Configure Credentials
#### Option 1: Using FastMCP OAuth Proxy

The server supports FastMCP's [OAuth proxy](https://gofastmcp.com/servers/auth/oauth-proxy) feature for dynamic user authentication. This is useful when running the server as a web service.

To enable it, set the following environment variables:

- `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID`: Your Google Cloud OAuth 2.0 Client ID.
- `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET`: Your Google Cloud OAuth 2.0 Client Secret.
- `GOOGLE_ADS_MCP_BASE_URL`: (Optional) The base URL where the server is accessible (defaults to `http://localhost:8080`).

Once this is enabled, you can authenticate to the API through your MCP client.

When these variables are set, the server automatically switches to the `streamable-http` transport (SSE/HTTP) instead of `stdio`.

You will need to run the server as a separate process and configure your MCP client to connect to the SSE endpoint (e.g., `http://localhost:8080/mcp`).

#### Option 2: Configure credentials using Application Default Credentials

Configure your [Application Default Credentials
(ADC)](https://cloud.google.com/docs/authentication/provide-credentials-adc).
Make sure the credentials are for a user with access to your Google Ads
accounts or properties.

Credentials must include the Google Ads API scope:

```
https://www.googleapis.com/auth/adwords
```

Check out
[Manage OAuth Clients](https://support.google.com/cloud/answer/15549257)
for how to create an OAuth client.

Here are some sample `gcloud` commands you might find useful:


- Set up ADC using user credentials and an OAuth desktop or web client after
  downloading the client JSON to `YOUR_CLIENT_JSON_FILE`.

  ```shell
  gcloud auth application-default login \
    --scopes https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform \
    --client-id-file=YOUR_CLIENT_JSON_FILE
  ```

- Set up ADC using service account impersonation.

  ```shell
  gcloud auth application-default login \
    --impersonate-service-account=SERVICE_ACCOUNT_EMAIL \
    --scopes=https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform
  ```

When the `gcloud auth application-default` command completes, copy the
`PATH_TO_CREDENTIALS_JSON` file location printed to the console in the
following message. You will need this for a later step!

```
Credentials saved to file: [PATH_TO_CREDENTIALS_JSON]
```

#### Option 3: Configure credentials using the Google Ads API Python client library.

[Follow the instructions](https://developers.google.com/google-ads/api/docs/client-libs/python/)
to setup and configure the Google Ads API Python client library

Note that this server does not read `google-ads.yaml` directly: it
authenticates through Application Default Credentials and reads the developer
token and login customer id from environment variables (see the MCP client
configuration below). If you already set up the client library, you can reuse
the same OAuth client and values in those environment variables.

### Configure your MCP client

Add the server to your MCP client's configuration. The `mcpServers` block
format below is the same across MCP clients; refer to your client's
documentation for where its configuration file lives.

- Option 1: Using FastMCP OAuth Proxy (Streamable HTTP)

  You can run the server as a separate process and configure your MCP client to connect to the SSE endpoint (e.g., `http://localhost:8080/mcp`).
  This also allows using FastMCP's [OAuth proxy](https://gofastmcp.com/servers/auth/oauth-proxy) feature for dynamic user authentication.

    ```json
    {
      "mcpServers": {
        "google-ads-mcp": {
          "httpUrl":"http://localhost:8080/mcp",
          "env": {
            "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
            "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN"                        
          }
        }
      }
    }
    ```

- Option 2: the Application Default Credentials method

    Replace `PATH_TO_CREDENTIALS_JSON` with the path you copied in the previous
    step.

    We also recommend that you add a `GOOGLE_CLOUD_PROJECT` attribute to the
    `env` object. Replace `YOUR_PROJECT_ID` in the following example with the
    [project ID](https://support.google.com/googleapi/answer/7014113) of your
    Google Cloud project.

    ```json
    {
      "mcpServers": {
        "google-ads-mcp": {
          "command": "pipx",
          "args": [
            "run",
            "--spec",
            "git+https://github.com/YOUR_ORG/google-ads-mcp-extended.git",
            "google-ads-mcp"
          ],
          "env": {
            "GOOGLE_APPLICATION_CREDENTIALS": "PATH_TO_CREDENTIALS_JSON",
            "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
            "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN"
          }
        }
      }
    }
    ```

- Option 3: the Python client library method

    ```json
    {
      "mcpServers": {
        "google-ads-mcp": {
          "command": "pipx",
          "args": [
            "run",
            "--spec",
            "git+https://github.com/YOUR_ORG/google-ads-mcp-extended.git",
            "google-ads-mcp"
          ],
          "env": {
            "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
            "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN"
          }
        }
      }
    }
    ```

#### Login Customer Id

If your access to the customer account is through a manager account, you will
need to add the customer ID of the manager account to the settings file.

See [here](https://developers.google.com/google-ads/api/docs/concepts/call-structure#cid) for details.

The final file will look like this:

  ```json
  {
    "mcpServers": {
      "google-ads-mcp": {
        "command": "pipx",
        "args": [
          "run",
          "--spec",
          "git+https://github.com/YOUR_ORG/google-ads-mcp-extended.git",
          "google-ads-mcp"
        ],
        "env": {
          "GOOGLE_APPLICATION_CREDENTIALS": "PATH_TO_CREDENTIALS_JSON",
          "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
          "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN",
          "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "YOUR_MANAGER_CUSTOMER_ID"
        }
      }
    }
  }
  ```

#### Where the configuration goes

The `mcpServers` block format is the same across MCP clients. Add the
configuration shown above to your client's MCP settings file, and replace
`YOUR_ORG` in the `--spec` argument with the repository you installed this
server from. If you run the server from a local checkout instead, point
`command` at the `google-ads-mcp` entry point in your virtual environment.

## Deployment to Google Cloud Platform

Instead of hosting this MCP server locally, you can host it on Google Cloud Run or on any other cloud-based infrastructure. This is useful if you want to share the server across different agents or run it as a web service.

Note that this only supports authentication with an OAuth Client ID and Client Secret pair through the OAuth proxy (Option #1 above).

### Prerequisites

1.  A Google Cloud project.
2.  The `gcloud` CLI installed, authenticated, and active project set.
    ```shell
    gcloud config set project YOUR_PROJECT_ID
    ```

### Step 1: Build and Push Docker Image

You can use Cloud Build to build and push the image to Artifact Registry without needing Docker installed locally.

1.  Create a repository in Artifact Registry:
    ```shell
    gcloud artifacts repositories create mcp-servers --repository-format=docker --location=us-central1
    ```
2.  Build and submit the image:
    ```shell
    gcloud builds submit --tag us-central1-docker.pkg.dev/YOUR_PROJECT_ID/mcp-servers/google-ads-mcp:latest .
    ```
    Replace `YOUR_PROJECT_ID` with your Google Cloud project ID.

### Step 2: Store secrets in Secret Manager

The developer token and the OAuth client secret are secrets. Do not pass them
via `--set-env-vars`: values passed that way end up in your shell history and
are visible in the Cloud Run revision configuration to anyone with the
`run.viewer` role. Store them in
[Secret Manager](https://cloud.google.com/secret-manager) and mount them with
`--set-secrets` instead.

1.  Create the secrets:
    ```shell
    printf '%s' 'YOUR_DEVELOPER_TOKEN' | \
      gcloud secrets create google-ads-developer-token --data-file=-
    printf '%s' 'YOUR_CLIENT_SECRET' | \
      gcloud secrets create google-ads-mcp-oauth-client-secret --data-file=-
    ```
2.  Grant the Cloud Run runtime service account access to them. By default
    this is the Compute Engine default service account
    (`PROJECT_NUMBER-compute@developer.gserviceaccount.com`); if you deploy
    with `--service-account`, grant that account instead.
    ```shell
    gcloud secrets add-iam-policy-binding google-ads-developer-token \
      --member="serviceAccount:RUNTIME_SERVICE_ACCOUNT" \
      --role="roles/secretmanager.secretAccessor"
    gcloud secrets add-iam-policy-binding google-ads-mcp-oauth-client-secret \
      --member="serviceAccount:RUNTIME_SERVICE_ACCOUNT" \
      --role="roles/secretmanager.secretAccessor"
    ```

Without the `roles/secretmanager.secretAccessor` binding, the deployment in
the next step fails to start.

### Step 3: Deploy to Google Cloud Run

Environment variables used by the server:

- `GOOGLE_PROJECT_ID`: Your Google Cloud project ID.
- `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID`: The OAuth Client ID you want the MCP server to use.
- `GOOGLE_ADS_MCP_BASE_URL`: The base URL where your MCP server is accessible: this will be automatically assigned by Google Cloud Run after your first deployment. You can update the environment variables after deployment.
- `GOOGLE_ADS_DEVELOPER_TOKEN` and `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET`: supplied from Secret Manager via `--set-secrets` (see Step 2).

```shell
gcloud run deploy google-ads-mcp \
  --image us-central1-docker.pkg.dev/YOUR_PROJECT_ID/mcp-servers/google-ads-mcp:latest \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars="GOOGLE_PROJECT_ID=YOUR_PROJECT_ID,GOOGLE_ADS_MCP_OAUTH_CLIENT_ID=YOUR_CLIENT_ID,GOOGLE_ADS_MCP_BASE_URL=YOUR_BASE_URL" \
  --set-secrets="GOOGLE_ADS_DEVELOPER_TOKEN=google-ads-developer-token:latest,GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET=google-ads-mcp-oauth-client-secret:latest"
```

Note on `--allow-unauthenticated`: it is required — the OAuth proxy flow must
be reachable by unauthenticated clients so they can complete the sign-in.
This means the OAuth flow is the **only** access control on the deployed
service: anyone who can reach the URL can attempt to authenticate, and any
Google account that completes the flow can call the tools (spending your
developer token quota). Treat the service URL accordingly, and consider
restricting ingress at the network level if your setup allows it.

### Step 4: Configure MCP Client

Once deployed, update your MCP client configuration to use the Cloud Run URL.

```json
{
  "mcpServers": {
    "google-ads-mcp": {
      "httpUrl": "https://your-cloud-run-url.a.run.app/mcp"
    }
  }
}
```

## Try it out

Launch your MCP client. You should see `google-ads-mcp` listed in the
available servers.

Here are some sample prompts to get you started:

- Ask what the server can do:

  ```
  what can the ads-mcp server do?
  ```

- Ask about customers:

  ```
  what customers do I have access to?
  ```

- Ask about campaigns 

  ```
  How many active campaigns do I have?
  ```

  ```
  How is my campaign performance this week?
  ```

### Note about Customer ID

Your agent will need and ask for a customer id for most prompts. If you are 
moving between multiple customers, including the customer ID in the prompt may
be simpler.

```
How many active campaigns do I have for customer id 1234567890
```

## Skills

This repository also provides [Agent Skills](https://agentskills.io/), which are specialized workflows and instructions that give AI agents specific expertise.

### Skills available

- `account-performance-diagnostics`: Diagnose account performance issues such as conversion loss, low lead flow, and lost opportunities. Located in `ads_mcp/skills/account-performance-diagnostics`.

### How to install skills

To use these skills, point your skills-compatible AI agent at the skill
directory: copy the folder into the agent's skills directory, or reference it
from there. Refer to your agent's documentation for the exact location.

Agent Skills are an open standard, so any agent or LLM tool that supports the
format can load them.


## Contributing

Contributions welcome! See the [Contributing Guide](CONTRIBUTING.md).
