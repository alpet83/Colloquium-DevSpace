const transport = normalizeModuleRunnerTransport((() => {
        let wsTransport = createWebSocketModuleRunnerTransport({
                createConnection: () => new WebSocket(`${socketProtocol}://${socketHost}?token=${wsToken}`, "vite-hmr"),
                pingInterval: hmrTimeout
        });
        return {
                async connect(handlers) {
                        try {
                                await wsTransport.connect(handlers);
                        } catch (e) {
                                if (!hmrPort) {
                                        wsTransport = createWebSocketModuleRunnerTransport({
                                                createConnection: () => new WebSocket(`${socketProtocol}://${directSocketHost}?token=${wsToken}`, "vite-hmr"),
                                                pingInterval: hmrTimeout
                                        });
                                        try {
                                                await wsTransport.connect(handlers);
                                                console.info("[vite] Direct websocket connection fallback. Check out https://vite.dev/config/server-options.html#server-hmr to remove the previous connection error.");
                                        } catch (e$1) {
                                                if (e$1 instanceof Error && e$1.message.includes("WebSocket closed without opened.")) {
                                                        const currentScriptHostURL = new URL(import.meta.url);
                                                        const currentScriptHost = currentScriptHostURL.host + currentScriptHostURL.pathname.replace(/@vite\/client$/, "");
                                                        console.error(`[vite] failed to connect to websocket.
your current setup:
  (browser) ${currentScriptHost} <--[HTTP]--> ${serverHost} (server)\n  (browser) ${socketHost} <--[WebSocket (failing)]--> ${directSocketHost} (server)\nCheck out your Vite / network configuration and https://vite.dev/config/server-options.html#server-hmr .`);
                                                }
                                        }
                                        return;
                                }
                                console.error(`[vite] failed to connect to websocket (${e}). `);
                                throw e;
                        }
                },
                async disconnect() {
                        await wsTransport.disconnect();
                },
                send(data) {
                        wsTransport.send(data);
                }
        };
})());