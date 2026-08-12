"""
=====================================================================================
 CONTROL DE INVENTARIO Y VENTAS - MERMELADAS (v2 - Google Sheets en vivo)
=====================================================================================
Aplicación Streamlit que usa una Google Sheet como base de datos "en vivo": cada venta,
cada cambio de stock y cada cierre de venta se escribe directamente en la planilla, sin
backups manuales. La planilla misma queda como respaldo (Google Sheets guarda su propio
historial de versiones en Archivo > Historial de versiones).

REQUIERE configurar 2 "Secrets" en Streamlit (ver guía de despliegue):
  - SHEET_ID               -> el ID de tu Google Sheet
  - [gcp_service_account]  -> las credenciales de una cuenta de servicio de Google Cloud

Estructura de datos (3 pestañas dentro de la misma Google Sheet, se crean solas la
primera vez que corre la app):
  - inventario:      id, sabor, stock, precio_costo, precio_venta
  - ventas:           id, fecha_venta, fecha_entrega, lugar, cliente, descuento,
                       subtotal_bruto, total, estado_pago, estado_entrega, estado_venta
  - detalle_ventas:   id, venta_id, sabor, cantidad, precio_unitario, subtotal
                       (una venta puede tener varias filas de detalle: varios productos
                       distintos en una misma boleta)
=====================================================================================
"""

import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from datetime import date

# -------------------------------------------------------------------------------------
# CONFIGURACIÓN GENERAL
# -------------------------------------------------------------------------------------
st.set_page_config(page_title="Control Mermeladas", page_icon="🍓", layout="wide")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

INVENTARIO_HEADERS = ["id", "sabor", "stock", "precio_costo", "precio_venta"]
INVENTARIO_NUM = ["id", "stock", "precio_costo", "precio_venta"]

VENTAS_HEADERS = [
    "id", "fecha_venta", "fecha_entrega", "lugar", "cliente", "descuento",
    "subtotal_bruto", "total", "estado_pago", "estado_entrega", "estado_venta",
]
VENTAS_NUM = ["id", "descuento", "subtotal_bruto", "total"]

DETALLE_HEADERS = ["id", "venta_id", "sabor", "cantidad", "precio_unitario", "subtotal"]
DETALLE_NUM = ["id", "venta_id", "cantidad", "precio_unitario", "subtotal"]

UMBRAL_STOCK_BAJO_DEFAULT = 5


# -------------------------------------------------------------------------------------
# CONEXIÓN A GOOGLE SHEETS
# -------------------------------------------------------------------------------------
@st.cache_resource
def get_gspread_client():
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


@st.cache_resource
def get_spreadsheet():
    client = get_gspread_client()
    return client.open_by_key(st.secrets["SHEET_ID"])


@st.cache_resource
def _worksheet_cache():
    return {}


def get_or_create_ws(nombre, headers):
    """Devuelve la pestaña (worksheet) indicada, creándola con sus encabezados si no existe."""
    cache = _worksheet_cache()
    if nombre in cache:
        return cache[nombre]
    sh = get_spreadsheet()
    try:
        ws = sh.worksheet(nombre)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=nombre, rows=2000, cols=max(len(headers), 10))
        ws.append_row(headers)
    cache[nombre] = ws
    return ws


def leer_hoja(nombre, headers, columnas_numericas=None):
    """Lee una pestaña completa de la Google Sheet y la devuelve como DataFrame."""
    ws = get_or_create_ws(nombre, headers)
    registros = ws.get_all_records()
    df = pd.DataFrame(registros)
    if df.empty:
        df = pd.DataFrame(columns=headers)
    else:
        for h in headers:
            if h not in df.columns:
                df[h] = None
        df = df[headers]
    if columnas_numericas:
        for col in columnas_numericas:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df


def escribir_hoja(nombre, headers, df):
    """Sobrescribe una pestaña completa con el contenido del DataFrame (guardado en vivo)."""
    ws = get_or_create_ws(nombre, headers)
    ws.clear()
    filas = [headers]
    if not df.empty:
        tmp = df[headers].copy()
        tmp = tmp.where(pd.notnull(tmp), "")
        filas += tmp.astype(str).values.tolist()
    ws.update(filas, value_input_option="USER_ENTERED")


def siguiente_id(df):
    if df.empty or "id" not in df.columns:
        return 1
    ids = pd.to_numeric(df["id"], errors="coerce").dropna()
    return int(ids.max()) + 1 if not ids.empty else 1


# -------------------------------------------------------------------------------------
# FUNCIONES - INVENTARIO
# -------------------------------------------------------------------------------------
def obtener_inventario():
    return leer_hoja("inventario", INVENTARIO_HEADERS, INVENTARIO_NUM)


def agregar_sabor(sabor, stock, precio_costo, precio_venta):
    df = obtener_inventario()
    if sabor.strip().lower() in df["sabor"].str.lower().values:
        return False, f"El sabor '{sabor}' ya existe en el inventario."
    nueva = {
        "id": siguiente_id(df), "sabor": sabor.strip(), "stock": stock,
        "precio_costo": precio_costo, "precio_venta": precio_venta,
    }
    df = pd.concat([df, pd.DataFrame([nueva])], ignore_index=True)
    escribir_hoja("inventario", INVENTARIO_HEADERS, df)
    return True, f"Sabor '{sabor}' agregado correctamente."


def actualizar_sabor(sabor_id, nuevo_stock, nuevo_costo, nuevo_venta):
    df = obtener_inventario()
    df.loc[df["id"] == sabor_id, ["stock", "precio_costo", "precio_venta"]] = [
        nuevo_stock, nuevo_costo, nuevo_venta,
    ]
    escribir_hoja("inventario", INVENTARIO_HEADERS, df)


# -------------------------------------------------------------------------------------
# FUNCIONES - VENTAS (con múltiples productos por venta)
# -------------------------------------------------------------------------------------
def obtener_ventas(estado=None):
    df = leer_hoja("ventas", VENTAS_HEADERS, VENTAS_NUM)
    if estado and not df.empty:
        df = df[df["estado_venta"] == estado]
    return df


def obtener_detalle(venta_id=None):
    df = leer_hoja("detalle_ventas", DETALLE_HEADERS, DETALLE_NUM)
    if venta_id is not None and not df.empty:
        df = df[df["venta_id"] == venta_id]
    return df


def registrar_venta_multiple(cliente, lugar, fecha_venta, fecha_entrega, descuento, carrito):
    """
    Crea una venta con uno o varios productos (carrito) en un solo registro:
    1 fila en 'ventas' (encabezado) + N filas en 'detalle_ventas' (una por producto).
    """
    ventas_df = leer_hoja("ventas", VENTAS_HEADERS, VENTAS_NUM)
    detalle_df = leer_hoja("detalle_ventas", DETALLE_HEADERS, DETALLE_NUM)

    nuevo_id_venta = siguiente_id(ventas_df)
    subtotal_bruto = sum(item["subtotal"] for item in carrito)
    total = max(subtotal_bruto - descuento, 0)

    nueva_venta = {
        "id": nuevo_id_venta,
        "fecha_venta": fecha_venta.strftime("%Y-%m-%d"),
        "fecha_entrega": fecha_entrega.strftime("%Y-%m-%d"),
        "lugar": lugar.strip(),
        "cliente": cliente.strip(),
        "descuento": descuento,
        "subtotal_bruto": subtotal_bruto,
        "total": total,
        "estado_pago": "Pendiente",
        "estado_entrega": "Pendiente",
        "estado_venta": "Abierta",
    }
    ventas_df = pd.concat([ventas_df, pd.DataFrame([nueva_venta])], ignore_index=True)

    siguiente_id_detalle = siguiente_id(detalle_df)
    filas_detalle = []
    for item in carrito:
        filas_detalle.append({
            "id": siguiente_id_detalle, "venta_id": nuevo_id_venta, "sabor": item["sabor"],
            "cantidad": item["cantidad"], "precio_unitario": item["precio_unitario"],
            "subtotal": item["subtotal"],
        })
        siguiente_id_detalle += 1
    detalle_df = pd.concat([detalle_df, pd.DataFrame(filas_detalle)], ignore_index=True)

    escribir_hoja("ventas", VENTAS_HEADERS, ventas_df)
    escribir_hoja("detalle_ventas", DETALLE_HEADERS, detalle_df)

    inventario_df = obtener_inventario()
    faltantes = []
    for item in carrito:
        stock_row = inventario_df.loc[inventario_df["sabor"] == item["sabor"], "stock"]
        disponible = stock_row.values[0] if len(stock_row) else 0
        if disponible < item["cantidad"]:
            faltantes.append(f"{item['sabor']} (disponible {int(disponible)}, vendido {item['cantidad']})")

    if faltantes:
        return nuevo_id_venta, (
            "Venta registrada. Atención, stock insuficiente para: " + "; ".join(faltantes) +
            ". Repón stock antes de poder cerrar esta venta."
        )
    return nuevo_id_venta, "Venta registrada correctamente."


def actualizar_estado_venta(venta_id, nuevo_pago, nueva_entrega):
    """
    Igual lógica de negocio que antes, pero ahora revisa el stock de TODOS los
    productos de la venta antes de cerrarla. Si falta stock de cualquiera de ellos,
    no cierra ninguno y avisa cuáles faltan.
    """
    ventas_df = leer_hoja("ventas", VENTAS_HEADERS, VENTAS_NUM)
    idx_list = ventas_df.index[ventas_df["id"] == venta_id]
    if idx_list.empty:
        return "error", "Venta no encontrada."
    idx = idx_list[0]

    if ventas_df.at[idx, "estado_venta"] == "Cerrada":
        return "error", "Esta venta ya está cerrada y no se puede modificar."

    ventas_df.at[idx, "estado_pago"] = nuevo_pago
    ventas_df.at[idx, "estado_entrega"] = nueva_entrega

    items = obtener_detalle(venta_id)

    if nuevo_pago == "Pagado" and nueva_entrega == "Entregado":
        inventario_df = obtener_inventario()
        faltantes = []
        for _, item in items.iterrows():
            stock_row = inventario_df.loc[inventario_df["sabor"] == item["sabor"], "stock"]
            disponible = stock_row.values[0] if len(stock_row) else 0
            if disponible < item["cantidad"]:
                faltantes.append(f"{item['sabor']} (disponible {int(disponible)}, requerido {int(item['cantidad'])})")

        if not faltantes:
            for _, item in items.iterrows():
                inventario_df.loc[inventario_df["sabor"] == item["sabor"], "stock"] -= item["cantidad"]
            ventas_df.at[idx, "estado_venta"] = "Cerrada"
            escribir_hoja("inventario", INVENTARIO_HEADERS, inventario_df)
            escribir_hoja("ventas", VENTAS_HEADERS, ventas_df)
            return "cerrada", f"Venta #{venta_id} cerrada y stock descontado correctamente."
        else:
            escribir_hoja("ventas", VENTAS_HEADERS, ventas_df)
            return "advertencia", (
                f"No se puede cerrar la venta #{venta_id}: falta stock de " + "; ".join(faltantes) +
                ". Repón stock en la pestaña Inventario e inténtalo de nuevo."
            )
    else:
        escribir_hoja("ventas", VENTAS_HEADERS, ventas_df)
        return "guardado", "Cambios guardados."


def obtener_detalle_ventas_cerradas():
    """Detalle de productos vendidos, solo de ventas ya cerradas (para rankings)."""
    ventas_df = obtener_ventas("Cerrada")[["id", "cliente", "fecha_venta"]].rename(columns={"id": "venta_id_ref"})
    detalle_df = leer_hoja("detalle_ventas", DETALLE_HEADERS, DETALLE_NUM)
    if ventas_df.empty or detalle_df.empty:
        return pd.DataFrame(columns=["venta_id", "sabor", "cantidad", "subtotal", "cliente", "fecha_venta"])
    return detalle_df.merge(ventas_df, left_on="venta_id", right_on="venta_id_ref", how="inner")


# -------------------------------------------------------------------------------------
# UTILIDADES
# -------------------------------------------------------------------------------------
def clp(valor):
    """Formatea un número como precio en pesos chilenos, ej: $12.500"""
    return "$" + f"{valor:,.0f}".replace(",", ".")


# -------------------------------------------------------------------------------------
# VERIFICAR CONEXIÓN ANTES DE MOSTRAR LA APP
# -------------------------------------------------------------------------------------
try:
    get_spreadsheet()
except Exception as e:
    st.error(
        "No se pudo conectar con Google Sheets. Revisa que hayas configurado los "
        "'Secrets' de la app (SHEET_ID y [gcp_service_account]) siguiendo la guía de despliegue, "
        "y que la planilla esté compartida con el correo de la cuenta de servicio."
    )
    st.caption(f"Detalle técnico: {e}")
    st.stop()

if "carrito" not in st.session_state:
    st.session_state.carrito = []

# -------------------------------------------------------------------------------------
# INTERFAZ - BARRA LATERAL
# -------------------------------------------------------------------------------------
st.sidebar.title("🍓 Control Mermeladas")
pagina = st.sidebar.radio("Ir a:", ["📊 Resumen", "📦 Inventario", "🧾 Ventas"])

# =======================================================================================
# PÁGINA: RESUMEN
# =======================================================================================
if pagina == "📊 Resumen":
    st.title("📊 Resumen del negocio")

    inventario_df = obtener_inventario()
    ventas_abiertas_df = obtener_ventas("Abierta")
    ventas_cerradas_df = obtener_ventas("Cerrada")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Unidades en stock", int(inventario_df["stock"].sum()) if not inventario_df.empty else 0)
    col2.metric("Ventas abiertas", len(ventas_abiertas_df))
    col3.metric("Ventas cerradas", len(ventas_cerradas_df))
    ingresos_totales = ventas_cerradas_df["total"].sum() if not ventas_cerradas_df.empty else 0
    col4.metric("Ingresos cobrados (histórico)", clp(ingresos_totales))

    st.divider()
    st.subheader("📅 Resumen mensual")

    if ventas_cerradas_df.empty:
        st.info("Todavía no hay ventas cerradas para mostrar un resumen mensual.")
    else:
        ventas_cerradas_df["fecha_venta_dt"] = pd.to_datetime(ventas_cerradas_df["fecha_venta"], errors="coerce")
        ventas_cerradas_df["mes"] = ventas_cerradas_df["fecha_venta_dt"].dt.to_period("M").astype(str)
        meses_disponibles = sorted(ventas_cerradas_df["mes"].dropna().unique(), reverse=True)
        mes_elegido = st.selectbox("Selecciona un mes (según fecha de venta)", meses_disponibles)

        df_mes = ventas_cerradas_df[ventas_cerradas_df["mes"] == mes_elegido]
        detalle_cerradas = obtener_detalle_ventas_cerradas()
        detalle_mes = detalle_cerradas[detalle_cerradas["venta_id"].isin(df_mes["id"])]

        c1, c2, c3 = st.columns(3)
        c1.metric("Ventas del mes", len(df_mes))
        c2.metric("Ingresos del mes", clp(df_mes["total"].sum()))
        c3.metric("Unidades vendidas del mes", int(detalle_mes["cantidad"].sum()) if not detalle_mes.empty else 0)

        cA, cB = st.columns(2)
        with cA:
            st.markdown("**Top 5 productos del mes**")
            if not detalle_mes.empty:
                top_prod_mes = (
                    detalle_mes.groupby("sabor")["cantidad"].sum()
                    .sort_values(ascending=False).head(5).reset_index()
                    .rename(columns={"sabor": "Sabor", "cantidad": "Unidades"})
                )
                st.dataframe(top_prod_mes, use_container_width=True, hide_index=True)
            else:
                st.caption("Sin datos este mes.")
        with cB:
            st.markdown("**Top 5 clientes del mes**")
            if not df_mes.empty:
                top_cli_mes = (
                    df_mes.groupby("cliente")["total"].sum()
                    .sort_values(ascending=False).head(5).reset_index()
                    .rename(columns={"cliente": "Cliente", "total": "Total comprado"})
                )
                top_cli_mes["Total comprado"] = top_cli_mes["Total comprado"].apply(clp)
                st.dataframe(top_cli_mes, use_container_width=True, hide_index=True)
            else:
                st.caption("Sin datos este mes.")

    st.divider()
    st.subheader("🏆 Rankings históricos")

    cA, cB = st.columns(2)
    with cA:
        st.markdown("**Top 20 clientes que más compran**")
        if ventas_cerradas_df.empty:
            st.caption("Todavía no hay ventas cerradas.")
        else:
            top_clientes = (
                ventas_cerradas_df.groupby("cliente")
                .agg(compras=("id", "count"), total_comprado=("total", "sum"))
                .sort_values("total_comprado", ascending=False)
                .head(20).reset_index()
                .rename(columns={"cliente": "Cliente", "compras": "N° compras", "total_comprado": "Total comprado"})
            )
            top_clientes["Total comprado"] = top_clientes["Total comprado"].apply(clp)
            st.dataframe(top_clientes, use_container_width=True, hide_index=True)

    with cB:
        st.markdown("**Top 20 productos más vendidos**")
        detalle_cerradas_todo = obtener_detalle_ventas_cerradas()
        if detalle_cerradas_todo.empty:
            st.caption("Todavía no hay ventas cerradas.")
        else:
            top_productos = (
                detalle_cerradas_todo.groupby("sabor")
                .agg(unidades_vendidas=("cantidad", "sum"), ingresos=("subtotal", "sum"))
                .sort_values("unidades_vendidas", ascending=False)
                .head(20).reset_index()
                .rename(columns={"sabor": "Sabor", "unidades_vendidas": "Unidades vendidas", "ingresos": "Ingresos"})
            )
            top_productos["Ingresos"] = top_productos["Ingresos"].apply(clp)
            st.dataframe(top_productos, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Ventas abiertas pendientes de gestionar")
    if ventas_abiertas_df.empty:
        st.info("No hay ventas abiertas por el momento.")
    else:
        st.dataframe(
            ventas_abiertas_df.drop(columns=["subtotal_bruto"], errors="ignore"),
            use_container_width=True, hide_index=True,
        )

    with st.expander("⬇️ Exportar un respaldo manual en Excel (opcional)"):
        st.caption(
            "Los datos ya viven de forma permanente en tu Google Sheet — esto es solo una "
            "copia adicional en Excel si la necesitas para contabilidad."
        )
        import io
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            obtener_inventario().to_excel(writer, sheet_name="inventario", index=False)
            leer_hoja("ventas", VENTAS_HEADERS, VENTAS_NUM).to_excel(writer, sheet_name="ventas", index=False)
            leer_hoja("detalle_ventas", DETALLE_HEADERS, DETALLE_NUM).to_excel(writer, sheet_name="detalle_ventas", index=False)
        buffer.seek(0)
        st.download_button(
            "Descargar Excel", data=buffer,
            file_name=f"export_mermeladas_{date.today().isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

# =======================================================================================
# PÁGINA: INVENTARIO
# =======================================================================================
elif pagina == "📦 Inventario":
    st.title("📦 Inventario de mermeladas")

    umbral = st.number_input(
        "Umbral de stock bajo (unidades) — por debajo de este número se marca en rojo",
        min_value=0, value=UMBRAL_STOCK_BAJO_DEFAULT, step=1,
    )

    with st.expander("➕ Agregar nuevo sabor", expanded=False):
        with st.form("form_agregar_sabor", clear_on_submit=True):
            c1, c2, c3, c4 = st.columns(4)
            nuevo_sabor = c1.text_input("Sabor / variedad")
            nuevo_stock = c2.number_input("Stock inicial", min_value=0, step=1, value=0)
            nuevo_costo = c3.number_input("Precio costo ($)", min_value=0, step=100, value=0)
            nuevo_venta = c4.number_input("Precio venta público ($)", min_value=0, step=100, value=0)
            enviado = st.form_submit_button("Agregar sabor")
            if enviado:
                if not nuevo_sabor.strip():
                    st.error("El nombre del sabor no puede estar vacío.")
                else:
                    ok, msg = agregar_sabor(nuevo_sabor, nuevo_stock, nuevo_costo, nuevo_venta)
                    st.success(msg) if ok else st.error(msg)
                    st.rerun()

    st.subheader("Stock actual")
    inventario_df = obtener_inventario()

    if inventario_df.empty:
        st.info("Todavía no hay sabores registrados. Agrega el primero arriba. ⬆️")
    else:
        tabla = inventario_df.copy()
        tabla["margen"] = tabla["precio_venta"] - tabla["precio_costo"]
        tabla = tabla.rename(columns={
            "sabor": "Sabor", "stock": "Stock", "precio_costo": "Precio costo",
            "precio_venta": "Precio venta", "margen": "Margen",
        })[["Sabor", "Stock", "Precio costo", "Precio venta", "Margen"]]
        for col in ["Precio costo", "Precio venta", "Margen"]:
            tabla[col] = tabla[col].apply(clp)

        def resaltar_stock_bajo(row):
            es_bajo = inventario_df.loc[inventario_df["sabor"] == row["Sabor"], "stock"].values[0] < umbral
            return ["background-color: #ffd6d6" if es_bajo else ""] * len(row)

        st.dataframe(tabla.style.apply(resaltar_stock_bajo, axis=1), use_container_width=True, hide_index=True)

        bajo_stock = inventario_df[inventario_df["stock"] < umbral]
        if not bajo_stock.empty:
            st.warning("⚠️ Sabores con stock bajo: " + ", ".join(bajo_stock["sabor"].tolist()))

        st.subheader("✏️ Editar stock o precios de un sabor")
        opciones = {f"{r.sabor} (stock actual: {r.stock})": r.id for r in inventario_df.itertuples()}
        seleccion = st.selectbox("Selecciona un sabor", list(opciones.keys()))
        sabor_id = opciones[seleccion]
        fila = inventario_df[inventario_df["id"] == sabor_id].iloc[0]

        with st.form("form_editar_sabor"):
            c1, c2, c3 = st.columns(3)
            stock_editado = c1.number_input("Nuevo stock", min_value=0, step=1, value=int(fila["stock"]))
            costo_editado = c2.number_input("Nuevo precio costo ($)", min_value=0, step=100, value=int(fila["precio_costo"]))
            venta_editado = c3.number_input("Nuevo precio venta ($)", min_value=0, step=100, value=int(fila["precio_venta"]))
            guardar = st.form_submit_button("Guardar cambios")
            if guardar:
                actualizar_sabor(sabor_id, stock_editado, costo_editado, venta_editado)
                st.success("Sabor actualizado correctamente.")
                st.rerun()

# =======================================================================================
# PÁGINA: VENTAS
# =======================================================================================
elif pagina == "🧾 Ventas":
    st.title("🧾 Seguimiento de ventas")

    inventario_df = obtener_inventario()

    st.subheader("➕ Registrar nueva venta")
    if inventario_df.empty:
        st.info("Primero agrega al menos un sabor en la pestaña 📦 Inventario.")
    else:
        st.markdown("**1. Agrega los productos de esta venta**")
        c1, c2, c3 = st.columns([3, 1, 1])
        sabor_sel = c1.selectbox("Sabor", inventario_df["sabor"].tolist(), key="sabor_carrito")
        cant_sel = c2.number_input("Cantidad", min_value=1, step=1, value=1, key="cant_carrito")
        if c3.button("➕ Agregar al carrito", use_container_width=True):
            precio = float(inventario_df.loc[inventario_df["sabor"] == sabor_sel, "precio_venta"].values[0])
            st.session_state.carrito.append({
                "sabor": sabor_sel, "cantidad": int(cant_sel),
                "precio_unitario": precio, "subtotal": precio * cant_sel,
            })
            st.rerun()

        if st.session_state.carrito:
            st.markdown("**Productos en esta venta:**")
            for i, item in enumerate(st.session_state.carrito):
                cc1, cc2, cc3, cc4, cc5 = st.columns([3, 1, 2, 2, 1])
                cc1.write(item["sabor"])
                cc2.write(item["cantidad"])
                cc3.write(clp(item["precio_unitario"]))
                cc4.write(clp(item["subtotal"]))
                if cc5.button("🗑️", key=f"del_item_{i}"):
                    st.session_state.carrito.pop(i)
                    st.rerun()
            subtotal_bruto_carrito = sum(x["subtotal"] for x in st.session_state.carrito)
            st.write(f"**Subtotal:** {clp(subtotal_bruto_carrito)}")
        else:
            st.info("Agrega al menos un producto al carrito antes de confirmar la venta.")

        st.markdown("**2. Datos de la venta y confirmación**")
        with st.form("form_confirmar_venta"):
            c1, c2 = st.columns(2)
            cliente = c1.text_input("Cliente")
            lugar = c2.text_input("Lugar (feria, domicilio, retiro, etc.)")
            c3, c4 = st.columns(2)
            fecha_venta = c3.date_input("Fecha de venta", value=date.today())
            fecha_entrega = c4.date_input("Fecha de entrega", value=date.today())
            descuento = st.number_input("Descuento ($)", min_value=0, step=100, value=0)
            confirmar = st.form_submit_button("Confirmar venta")

            if confirmar:
                if not st.session_state.carrito:
                    st.error("Agrega al menos un producto al carrito.")
                elif not cliente.strip():
                    st.error("El nombre del cliente no puede estar vacío.")
                else:
                    _, msg = registrar_venta_multiple(
                        cliente, lugar, fecha_venta, fecha_entrega, descuento, st.session_state.carrito
                    )
                    st.session_state.carrito = []
                    st.success(msg)
                    st.rerun()

    st.divider()
    st.subheader("🔄 Actualizar estado de una venta abierta")
    ventas_abiertas_df = obtener_ventas("Abierta")

    if ventas_abiertas_df.empty:
        st.info("No hay ventas abiertas en este momento.")
    else:
        opciones_venta = {
            f"#{int(r.id)} — {r.cliente} — {r.lugar} — {clp(r.total)}": int(r.id)
            for r in ventas_abiertas_df.itertuples()
        }
        seleccion_venta = st.selectbox("Selecciona una venta", list(opciones_venta.keys()))
        venta_id = opciones_venta[seleccion_venta]
        fila_venta = ventas_abiertas_df[ventas_abiertas_df["id"] == venta_id].iloc[0]

        detalle_venta = obtener_detalle(venta_id)
        with st.expander("Ver productos de esta venta", expanded=True):
            st.dataframe(
                detalle_venta[["sabor", "cantidad", "precio_unitario", "subtotal"]],
                use_container_width=True, hide_index=True,
            )
            st.caption(
                f"Fecha venta: {fila_venta['fecha_venta']} · Fecha entrega: {fila_venta['fecha_entrega']} · "
                f"Descuento: {clp(fila_venta['descuento'])} · Total: {clp(fila_venta['total'])}"
            )

        with st.form("form_actualizar_venta"):
            c1, c2 = st.columns(2)
            pago = c1.selectbox(
                "Estado de pago", ["Pendiente", "Pagado"],
                index=["Pendiente", "Pagado"].index(fila_venta["estado_pago"]),
            )
            entrega = c2.selectbox(
                "Estado de entrega", ["Pendiente", "Entregado"],
                index=["Pendiente", "Entregado"].index(fila_venta["estado_entrega"]),
            )
            guardar_venta = st.form_submit_button("Guardar cambios")
            if guardar_venta:
                estado, mensaje = actualizar_estado_venta(venta_id, pago, entrega)
                if estado == "cerrada":
                    st.success(mensaje)
                elif estado == "advertencia":
                    st.warning(mensaje)
                elif estado == "error":
                    st.error(mensaje)
                else:
                    st.info(mensaje)
                st.rerun()

        st.markdown("**Todas las ventas abiertas:**")
        st.dataframe(
            ventas_abiertas_df.drop(columns=["subtotal_bruto"], errors="ignore"),
            use_container_width=True, hide_index=True,
        )

    st.divider()
    st.subheader("📜 Historial de ventas cerradas")
    ventas_cerradas_df = obtener_ventas("Cerrada")
    if ventas_cerradas_df.empty:
        st.info("Todavía no hay ventas cerradas.")
    else:
        st.dataframe(
            ventas_cerradas_df.drop(columns=["subtotal_bruto"], errors="ignore"),
            use_container_width=True, hide_index=True,
        )
