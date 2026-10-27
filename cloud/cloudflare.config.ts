import { bindings, defineConfig, triggers } from "cf/config";
import * as entrypoint from "./src/index.ts" with { type: "cf-worker" };

export default defineConfig({
	worker: {
		name: "drive-archotype-mac",
		compatibilityDate: "2026-10-27",
		entrypoint,
		observability: {
			enabled: true,
			traces: {
				enabled: true,
			},
		},
		triggers: [
			triggers.scheduled({ schedule: "0 */4 * * *" }),
		],
		env: {
			DB: bindings.d1({ name: "drive-archotype-db" }),
			PANEL_PIN: bindings.secret(),
			GOOGLE_CLIENT_ID: bindings.secret(),
			GOOGLE_CLIENT_SECRET: bindings.secret(),
			REFRESH_TOKEN_CUENTA1: bindings.secret(),
			REFRESH_TOKEN_CUENTA2: bindings.secret(),
		},
	},
});
