// Thin client: all logic lives in the Python language server (`logsite serve`).
import { execFile } from "child_process";
import * as vscode from "vscode";
import { LanguageClient, LanguageClientOptions, ServerOptions } from "vscode-languageclient/node";

const INSTALL = 'pip install "logsite[lsp]"';

let client: LanguageClient | undefined;
let output: vscode.OutputChannel;
let python: string | undefined; // the interpreter the running server was found through
let queue: Promise<void> = Promise.resolve(); // starts and restarts, one at a time

/** Resolves to true if `command args...` exits 0 within a few seconds. */
function runs(command: string, args: string[]): Promise<boolean> {
  return new Promise((resolve) => {
    execFile(command, args, { timeout: 10_000 }, (error) => resolve(!error));
  });
}

/** The Python extension's API, if it's installed. */
async function pythonApi(): Promise<any> {
  const ext = vscode.extensions.getExtension("ms-python.python");
  if (!ext) {
    return undefined;
  }
  return ext.isActive ? ext.exports : await ext.activate();
}

/** The interpreter selected in the Python extension, if it's installed. */
async function selectedPython(resource?: vscode.Uri): Promise<string | undefined> {
  try {
    const envs = (await pythonApi())?.environments;
    const active = envs?.getActiveEnvironmentPath(resource);
    const env = active && (await envs.resolveEnvironment(active));
    return env?.executable?.uri?.fsPath ?? active?.path;
  } catch (err) {
    output.appendLine(`Couldn't ask the Python extension for an interpreter: ${err}`);
    return undefined;
  }
}

/**
 * Where to run the server from, most specific first:
 * 1. the `logsite.serverCommand` setting,
 * 2. the Python interpreter selected in VS Code (`python -m logsite serve`), so a project's
 *    virtualenv works without extra setup,
 * 3. `logsite` on PATH.
 */
async function findServer(): Promise<{ server?: string[]; python?: string }> {
  const configured = vscode.workspace.getConfiguration("logsite").get<string[]>("serverCommand");
  if (configured && configured.length > 0) {
    return { server: configured };
  }
  const python = await selectedPython(vscode.workspace.workspaceFolders?.[0]?.uri);
  if (python && (await runs(python, ["-c", "import logsite.server"]))) {
    return { server: [python, "-m", "logsite", "serve"], python };
  }
  if (await runs("logsite", ["--version"])) {
    return { server: ["logsite", "serve"], python };
  }
  output.appendLine(
    `logsite not found${python ? ` in ${python}` : ""} or on PATH. Install it with: ${INSTALL}`,
  );
  return { python };
}

async function notFound(python?: string): Promise<void> {
  const select = "Select interpreter";
  const copy = "Copy install command";
  const settings = "Set server command";
  const message = python
    ? `logsite isn't installed in the selected Python (${python}). Select the interpreter of ` +
      `the environment where it's installed, or install it there: ${INSTALL}`
    : `logsite: the language server isn't installed. Install it in your project's environment: ${INSTALL}`;
  const choice = await vscode.window.showWarningMessage(message, select, copy, settings);
  if (choice === select) {
    await vscode.commands.executeCommand("python.setInterpreter");
  } else if (choice === copy) {
    await vscode.env.clipboard.writeText(INSTALL);
  } else if (choice === settings) {
    await vscode.commands.executeCommand("workbench.action.openSettings", "logsite.serverCommand");
  }
}

async function start(): Promise<void> {
  const found = await findServer();
  python = found.python;
  const server = found.server;
  if (!server) {
    await notFound(python);
    return;
  }
  const [command, ...args] = server;
  output.appendLine(`Starting: ${server.join(" ")}`);
  const serverOptions: ServerOptions = { command, args };
  const clientOptions: LanguageClientOptions = {
    documentSelector: [
      "python",
      "java",
      "go",
      "typescript",
      "typescriptreact",
      "javascript",
      "javascriptreact",
    ].map((language) => ({ scheme: "file", language })),
    outputChannel: output,
  };
  client = new LanguageClient("logsite", "logsite", serverOptions, clientOptions);
  try {
    await client.start();
  } catch (err) {
    output.appendLine(`The server failed to start: ${err}`);
    client = undefined;
    const show = "Show output";
    if ((await vscode.window.showErrorMessage(`logsite failed to start: ${err}`, show)) === show) {
      output.show();
    }
  }
}

/**
 * Stop the running server (if any) and start again. Queued, so events that arrive while a
 * server is starting (the Python extension fires several at startup) never start a second one.
 */
function restart(): Promise<void> {
  queue = queue
    .then(async () => {
      const old = client;
      client = undefined;
      await old?.stop();
      await start();
    })
    .catch((err) => output.appendLine(`Restart failed: ${err}`));
  return queue;
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  output = vscode.window.createOutputChannel("logsite");
  context.subscriptions.push(
    output,
    vscode.commands.registerCommand("logsite.restart", restart),
    vscode.commands.registerCommand("logsite.showOutput", () => output.show()),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("logsite.serverCommand")) {
        void restart();
      }
    }),
  );
  try {
    // Restart when the user picks another interpreter, e.g. after "Select interpreter".
    const envs = (await pythonApi())?.environments;
    const sub = envs?.onDidChangeActiveEnvironmentPath?.(async () => {
      if ((await selectedPython(vscode.workspace.workspaceFolders?.[0]?.uri)) !== python) {
        void restart();
      }
    });
    if (sub) {
      context.subscriptions.push(sub);
    }
  } catch (err) {
    output.appendLine(`Couldn't watch the Python interpreter: ${err}`);
  }
  await restart();
}

export async function deactivate(): Promise<void> {
  await client?.stop();
}
