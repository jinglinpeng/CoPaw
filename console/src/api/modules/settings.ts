import { request } from "../request";

export interface SettingsLanguage {
  language: string;
}

export const settingsApi = {
  getLanguage: () => request<SettingsLanguage>("/settings/language"),

  updateLanguage: (language: string) =>
    request<SettingsLanguage>("/settings/language", {
      method: "PUT",
      body: JSON.stringify({ language }),
    }),
};
