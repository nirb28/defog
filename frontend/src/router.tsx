// A very small router: the app has a handful of pages, so a library isn't worth it.
import { useEffect, useState, type AnchorHTMLAttributes, type MouseEvent } from "react";

const EVENT = "defog:navigate";

export function navigate(to: string, { replace = false } = {}) {
  if (replace) window.history.replaceState(null, "", to);
  else window.history.pushState(null, "", to);
  window.dispatchEvent(new Event(EVENT));
}

export interface Route {
  path: string;
  params: URLSearchParams;
}

function current(): Route {
  return { path: window.location.pathname.replace(/\/+$/, "") || "/", params: new URLSearchParams(window.location.search) };
}

export function useRoute(): Route {
  const [route, setRoute] = useState(current);
  useEffect(() => {
    const update = () => setRoute(current());
    window.addEventListener("popstate", update);
    window.addEventListener(EVENT, update);
    return () => {
      window.removeEventListener("popstate", update);
      window.removeEventListener(EVENT, update);
    };
  }, []);
  return route;
}

export function Link({ href, onClick, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) {
  function handle(e: MouseEvent<HTMLAnchorElement>) {
    onClick?.(e);
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    navigate(href);
  }
  return <a href={href} onClick={handle} {...rest} />;
}
