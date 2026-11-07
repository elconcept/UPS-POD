/* Set theme before styles are painted; respect cookie, default to light. */
(() => {
  const cookie = document.cookie.split("; ").find((item) => item.startsWith("ups_pod_theme="));
  document.documentElement.dataset.theme = cookie && cookie.slice("ups_pod_theme=".length) === "dark"
    ? "dark" : "light";
})();
