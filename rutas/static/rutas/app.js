// Compartido por todas las páginas — tema claro/oscuro y helpers de fetch.

function initTema() {
  const guardado = localStorage.getItem("rutas-tema");
  if (guardado === "light" || guardado === "dark") {
    document.documentElement.setAttribute("data-theme", guardado);
  }
}
initTema();

function pintarToggleTema() {
  const actual = document.documentElement.getAttribute("data-theme");
  const btnClaro = document.getElementById("btn-tema-claro");
  const btnOscuro = document.getElementById("btn-tema-oscuro");
  if (!btnClaro || !btnOscuro) return;
  btnClaro.classList.toggle("on", actual === "light");
  btnOscuro.classList.toggle("on", actual === "dark");
}

document.addEventListener("DOMContentLoaded", () => {
  pintarToggleTema();
  const btnClaro = document.getElementById("btn-tema-claro");
  const btnOscuro = document.getElementById("btn-tema-oscuro");
  if (btnClaro) btnClaro.addEventListener("click", () => {
    document.documentElement.setAttribute("data-theme", "light");
    localStorage.setItem("rutas-tema", "light");
    pintarToggleTema();
  });
  if (btnOscuro) btnOscuro.addEventListener("click", () => {
    document.documentElement.setAttribute("data-theme", "dark");
    localStorage.setItem("rutas-tema", "dark");
    pintarToggleTema();
  });
});

function getCookie(name) {
  const value = `; ${document.cookie}`;
  const parts = value.split(`; ${name}=`);
  if (parts.length === 2) return parts.pop().split(";").shift();
}

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken")},
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    const detalle = data && data.errores ? data.errores.join(" | ") : `HTTP ${resp.status}`;
    throw new Error(detalle);
  }
  return data;
}

// ── Columna de borrar fila, para agregar a cualquier tabla Tabulator ──
function columnaBorrar() {
  return {
    title: "", field: "_borrar", width: 42, hozAlign: "center",
    headerSort: false, formatter: () => "<button type='button' class='btn-del-fila' title='Borrar fila'>✕</button>",
    cellClick: (e, cell) => cell.getRow().delete(),
  };
}

// ── Importar CSV a una tabla Tabulator (encabezados flexibles) ──
function parseCSV(texto) {
  const filas = [];
  let fila = [], campo = "", entreComillas = false;
  for (let i = 0; i < texto.length; i++) {
    const c = texto[i], sig = texto[i + 1];
    if (entreComillas) {
      if (c === '"' && sig === '"') { campo += '"'; i++; }
      else if (c === '"') { entreComillas = false; }
      else { campo += c; }
    } else if (c === '"') {
      entreComillas = true;
    } else if (c === ",") {
      fila.push(campo); campo = "";
    } else if (c === "\r") {
      // ignorar
    } else if (c === "\n") {
      fila.push(campo); filas.push(fila); fila = []; campo = "";
    } else {
      campo += c;
    }
  }
  if (campo.length || fila.length) { fila.push(campo); filas.push(fila); }
  return filas.filter(f => !(f.length === 1 && f[0].trim() === ""));
}

function normalizarClave(s) {
  return s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

function importarCSV(inputId, camposNumericos, aliases, onListo) {
  const input = document.getElementById(inputId);
  if (!input) return;
  input.addEventListener("change", (e) => {
    const archivo = e.target.files[0];
    if (!archivo) return;
    const lector = new FileReader();
    lector.onload = () => {
      const filas = parseCSV(lector.result);
      if (filas.length < 2) return;
      const encabezados = filas[0].map(h => aliases[normalizarClave(h)] || null);
      const nuevasFilas = filas.slice(1).map(valores => {
        const fila = {};
        encabezados.forEach((campo, i) => {
          if (!campo) return;
          let valor = (valores[i] || "").trim();
          fila[campo] = camposNumericos.includes(campo) ? (valor === "" ? null : Number(valor)) : valor;
        });
        return fila;
      }).filter(f => Object.keys(f).length > 0);
      onListo(nuevasFilas);
      e.target.value = "";
    };
    lector.readAsText(archivo, "UTF-8");
  });
}
