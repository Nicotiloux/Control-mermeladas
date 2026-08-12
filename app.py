"""
=====================================================================================
 CONTROL DE INVENTARIO Y VENTAS - MERMELADAS (v3 - Google Sheets en vivo)
=====================================================================================
Cambios respecto a la v2:
  - Se puede editar y eliminar una venta ABIERTA (todavía no descontó stock).
  - Se puede anular una venta CERRADA: repone el stock y la excluye de los reportes.
  - Inventario ahora muestra también "necesidades de producción" según pedidos abiertos,
    además del stock físico (el stock se sigue usando internamente para el cierre
    automático de ventas).
  - Corregido: el descuento de una venta ahora se prorratea correctamente entre sus
    productos en el ranking histórico (antes el ranking mostraba el precio sin descuento).
  - Resumen mensual separa "ingresos cobrados" de "ingresos líquidos" (restando el
    precio costo de cada sabor vendido, según lo que definiste en Inventario).
  - El cliente se elige de una lista de clientes ya registrados (con opción de agregar
    uno nuevo) para evitar nombres duplicados con variaciones/errores de tipeo.
  - El sabor, al registrar una venta, se elige de una lista fija predefinida (no del
    inventario actual), ordenada alfabéticamente.

Estructura de datos en la Google Sheet (sin cambios respecto a v2, 100% compatible):
  - inventario:      id, sabor, stock, precio_costo, precio_venta
  - ventas:           id, fecha_venta, fecha_entrega, lugar, cliente, descuento,
                       subtotal_bruto, total, estado_pago, estado_entrega, estado_venta
                       (estado_venta ahora puede ser: Abierta / Cerrada / Anulada)
  - detalle_ventas:   id, venta_id, sabor, cantidad, precio_unitario, subtotal
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

# Lista fija de sabores para los formularios (ordenada alfabéticamente).
# Para agregar o quitar un sabor de esta lista, edita este arreglo.
SABORES_DISPONIBLES = sorted([
    "Alcayota", "Alcayota Nuez", "Ciruela", "Damasco", "Dulce Membrillo", "Durazno",
    "Frutilla", "Frutos Rojos", "Higo", "Higo Nuez", "Manjar", "Manjar Lucuma",
    "Manjar Nuez", "Melon", "Naranja", "Naranja Limon", "Papaya", "Papaya Piña",
])


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


def actualizar_sabor(sabor_id, nuevo_stock, nuevo_costo, nuevo_venta):
    df = obtener_inventario()
    df.loc[df["id"] == sabor_id, ["stock", "precio_costo", "precio_venta"]] = [
        nuevo_stock, nuevo_costo, nuevo_venta,
    ]
    escribir_hoja("inventario", INVENTARIO_HEADERS, df)


def asegurar_sabores_base():
    """
    Garantiza que los 18 sabores de SABORES_DISPONIBLES existan siempre como filas en
    Inventario (con stock y precios en 0 si son nuevos). Así nunca se bloquea registrar
    una venta de un sabor que todavía no tiene stock/precio cargado: primero se vende
    (el pedido queda registrado) y después Inventario te dice qué falta producir.
    """
    df = obtener_inventario()
    existentes = set(df["sabor"].tolist()) if not df.empty else set()
    faltantes = [s for s in SABORES_DISPONIBLES if s not in existentes]
    if faltantes:
        siguiente = siguiente_id(df)
        nuevas_filas = []
        for s in faltantes:
            nuevas_filas.append({"id": siguiente, "sabor": s, "stock": 0, "precio_costo": 0, "precio_venta": 0})
            siguiente += 1
        df = pd.concat([df, pd.DataFrame(nuevas_filas)], ignore_index=True)
        escribir_hoja("inventario", INVENTARIO_HEADERS, df)


def obtener_comprometido_por_sabor():
    """Unidades comprometidas en ventas ABIERTAS (todavía no descuentan stock), por sabor."""
    ventas_abiertas_df = obtener_ventas("Abierta")
    detalle_df = leer_hoja("detalle_ventas", DETALLE_HEADERS, DETALLE_NUM)
    if ventas_abiertas_df.empty or detalle_df.empty:
        return pd.Series(dtype=float)
    detalle_abiertas = detalle_df[detalle_df["venta_id"].isin(ventas_abiertas_df["id"])]
    return detalle_abiertas.groupby("sabor")["cantidad"].sum()


# -------------------------------------------------------------------------------------
# FUNCIONES - VENTAS
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
            ". Repón stock antes de cerrar esta venta."
        )
    return nuevo_id_venta, "Venta registrada correctamente."


def actualizar_venta_completa(venta_id, cliente, lugar, fecha_venta, fecha_entrega, descuento, carrito):
    """Reemplaza productos y datos generales de una venta ABIERTA existente (edición)."""
    ventas_df = leer_hoja("ventas", VENTAS_HEADERS, VENTAS_NUM)
    idx_list = ventas_df.index[ventas_df["id"] == venta_id]
    if idx_list.empty:
        return False, "Venta no encontrada."
    idx = idx_list[0]
    if ventas_df.at[idx, "estado_venta"] != "Abierta":
        return False, "Solo se pueden editar ventas que todavía están abiertas."

    subtotal_bruto = sum(item["subtotal"] for item in carrito)
    total = max(subtotal_bruto - descuento, 0)

    ventas_df.at[idx, "fecha_venta"] = fecha_venta.strftime("%Y-%m-%d")
    ventas_df.at[idx, "fecha_entrega"] = fecha_entrega.strftime("%Y-%m-%d")
    ventas_df.at[idx, "lugar"] = lugar.strip()
    ventas_df.at[idx, "cliente"] = cliente.strip()
    ventas_df.at[idx, "descuento"] = descuento
    ventas_df.at[idx, "subtotal_bruto"] = subtotal_bruto
    ventas_df.at[idx, "total"] = total

    detalle_df = leer_hoja("detalle_ventas", DETALLE_HEADERS, DETALLE_NUM)
    detalle_df = detalle_df[detalle_df["venta_id"] != venta_id]
    siguiente = siguiente_id(detalle_df)
    filas_nuevas = []
    for item in carrito:
        filas_nuevas.append({
            "id": siguiente, "venta_id": venta_id, "sabor": item["sabor"],
            "cantidad": item["cantidad"], "precio_unitario": item["precio_unitario"],
            "subtotal": item["subtotal"],
        })
        siguiente += 1
    detalle_df = pd.concat([detalle_df, pd.DataFrame(filas_nuevas)], ignore_index=True)

    escribir_hoja("ventas", VENTAS_HEADERS, ventas_df)
    escribir_hoja("detalle_ventas", DETALLE_HEADERS, detalle_df)
    return True, f"Venta #{venta_id} actualizada correctamente."


def eliminar_venta_abierta(venta_id):
    ventas_df = leer_hoja("ventas", VENTAS_HEADERS, VENTAS_NUM)
    fila = ventas_df[ventas_df["id"] == venta_id]
    if fila.empty or fila.iloc[0]["estado_venta"] != "Abierta":
        return False, "Solo se pueden eliminar ventas que están abiertas."
    ventas_df = ventas_df[ventas_df["id"] != venta_id]
    detalle_df = leer_hoja("detalle_ventas", DETALLE_HEADERS, DETALLE_NUM)
    detalle_df = detalle_df[detalle_df["venta_id"] != venta_id]
    escribir_hoja("ventas", VENTAS_HEADERS, ventas_df)
    escribir_hoja("detalle_ventas", DETALLE_HEADERS, detalle_df)
    return True, f"Venta #{venta_id} eliminada."


def anular_venta_cerrada(venta_id):
    """Anula una venta CERRADA: repone el stock descontado y la excluye de los reportes."""
    ventas_df = leer_hoja("ventas", VENTAS_HEADERS, VENTAS_NUM)
    idx_list = ventas_df.index[ventas_df["id"] == venta_id]
    if idx_list.empty:
        return False, "Venta no encontrada."
    idx = idx_list[0]
    if ventas_df.at[idx, "estado_venta"] != "Cerrada":
        return False, "Solo se pueden anular ventas que están cerradas."

    items = obtener_detalle(venta_id)
    inventario_df = obtener_inventario()
    for _, item in items.iterrows():
        inventario_df.loc[inventario_df["sabor"] == item["sabor"], "stock"] += item["cantidad"]
    escribir_hoja("inventario", INVENTARIO_HEADERS, inventario_df)

    ventas_df.at[idx, "estado_venta"] = "Anulada"
    escribir_hoja("ventas", VENTAS_HEADERS, ventas_df)
    return True, f"Venta #{venta_id} anulada. Se repusieron {int(items['cantidad'].sum())} unidades al stock."


def actualizar_estado_venta(venta_id, nuevo_pago, nueva_entrega):
    ventas_df = leer_hoja("ventas", VENTAS_HEADERS, VENTAS_NUM)
    idx_list = ventas_df.index[ventas_df["id"] == venta_id]
    if idx_list.empty:
        return "error", "Venta no encontrada."
    idx = idx_list[0]

    if ventas_df.at[idx, "estado_venta"] != "Abierta":
        return "error", "Esta venta ya no está abierta y no se puede modificar."

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
                ". Repón stock en Inventario e inténtalo de nuevo."
            )
    else:
        escribir_hoja("ventas", VENTAS_HEADERS, ventas_df)
        return "guardado", "Cambios guardados."


def obtener_detalle_ventas_cerradas():
    """
    Detalle de productos de ventas CERRADAS, con el descuento de cada venta ya
    prorrateado entre sus productos (columna 'subtotal_neto'), para que los rankings
    de ingresos reflejen el precio real cobrado y no el precio de lista.
    """
    ventas_df = obtener_ventas("Cerrada")
    columnas_venta = ["id", "cliente", "fecha_venta", "subtotal_bruto", "total"]
    ventas_df = ventas_df[columnas_venta].rename(columns={"id": "venta_id_ref"})
    detalle_df = leer_hoja("detalle_ventas", DETALLE_HEADERS, DETALLE_NUM)
    if ventas_df.empty or detalle_df.empty:
        return pd.DataFrame(columns=["venta_id", "sabor", "cantidad", "subtotal", "subtotal_neto", "cliente", "fecha_venta"])

    merged = detalle_df.merge(ventas_df, left_on="venta_id", right_on="venta_id_ref", how="inner")
    merged["factor_neto"] = merged.apply(
        lambda r: (r["total"] / r["subtotal_bruto"]) if r["subtotal_bruto"] > 0 else 1.0, axis=1
    )
    merged["subtotal_neto"] = merged["subtotal"] * merged["factor_neto"]
    return merged


# -------------------------------------------------------------------------------------
# UTILIDADES
# -------------------------------------------------------------------------------------
def clp(valor):
    return "$" + f"{valor:,.0f}".replace(",", ".")


# -------------------------------------------------------------------------------------
# VERIFICAR CONEXIÓN
# -------------------------------------------------------------------------------------
try:
    get_spreadsheet()
except Exception as e:
    st.error(
        "No se pudo conectar con Google Sheets. Revisa los 'Secrets' de la app "
        "(SHEET_ID y [gcp_service_account]) y que la planilla esté compartida con la "
        "cuenta de servicio."
    )
    st.caption(f"Detalle técnico: {e}")
    st.stop()

for clave, valor_inicial in [
    ("carrito", []), ("editando_venta_id", None),
    ("confirmar_eliminar_id", None), ("confirmar_anular_id", None),
]:
    if clave not in st.session_state:
        st.session_state[clave] = valor_inicial

if not st.session_state.get("sabores_verificados"):
    asegurar_sabores_base()
    st.session_state["sabores_verificados"] = True

# -------------------------------------------------------------------------------------
# BARRA LATERAL
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
        c2.metric("Ingresos cobrados del mes", clp(df_mes["total"].sum()))
        c3.metric("Unidades vendidas del mes", int(detalle_mes["cantidad"].sum()) if not detalle_mes.empty else 0)

        costos_por_sabor = inventario_df.set_index("sabor")["precio_costo"].to_dict()
        if not detalle_mes.empty:
            costo_mes = sum(row["cantidad"] * costos_por_sabor.get(row["sabor"], 0) for _, row in detalle_mes.iterrows())
        else:
            costo_mes = 0
        ingresos_liquidos_mes = df_mes["total"].sum() - costo_mes

        c4, c5 = st.columns(2)
        c4.metric("Costo de productos vendidos", clp(costo_mes))
        c5.metric("Ingresos líquidos del mes (ganancia)", clp(ingresos_liquidos_mes))
        st.caption(
            "El costo usa el 'precio costo' que tienes cargado hoy en Inventario para cada "
            "sabor. Si tus costos cambiaron con el tiempo, los meses pasados se recalculan "
            "con el costo actual."
        )

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
                .agg(unidades_vendidas=("cantidad", "sum"), ingresos=("subtotal_neto", "sum"))
                .sort_values("unidades_vendidas", ascending=False)
                .head(20).reset_index()
                .rename(columns={"sabor": "Sabor", "unidades_vendidas": "Unidades vendidas", "ingresos": "Ingresos (con descuento aplicado)"})
            )
            top_productos["Ingresos (con descuento aplicado)"] = top_productos["Ingresos (con descuento aplicado)"].apply(clp)
            st.dataframe(top_productos, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Ventas abiertas pendientes de gestionar")
    if ventas_abiertas_df.empty:
        st.info("No hay ventas abiertas por el momento.")
    else:
        st.dataframe(ventas_abiertas_df.drop(columns=["subtotal_bruto"], errors="ignore"), use_container_width=True, hide_index=True)

    with st.expander("⬇️ Exportar un respaldo manual en Excel (opcional)"):
        st.caption("Los datos ya viven en tu Google Sheet — esto es solo una copia adicional en Excel si la necesitas.")
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

    inventario_df = obtener_inventario()

    st.subheader("Stock actual")
    st.caption(
        "Los 18 sabores de tu catálogo están siempre disponibles para vender, tengan o no "
        "stock o precio cargado todavía. Usa el formulario de abajo para actualizarlos. "
        "Para agregar o quitar sabores del catálogo, edita SABORES_DISPONIBLES en el código."
    )
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

        st.divider()
        st.subheader("📋 Necesidades de producción según pedidos abiertos")
        st.caption(
            "Compara tu stock listo con lo comprometido en ventas abiertas (pagadas o no, "
            "entregadas o no) para saber qué te falta preparar."
        )
        comprometido = obtener_comprometido_por_sabor()
        tabla_prod = inventario_df[["sabor", "stock"]].copy()
        tabla_prod["comprometido"] = tabla_prod["sabor"].map(comprometido).fillna(0).astype(int)
        tabla_prod["a_producir"] = (tabla_prod["comprometido"] - tabla_prod["stock"]).clip(lower=0).astype(int)
        tabla_prod = tabla_prod.rename(columns={
            "sabor": "Sabor", "stock": "Stock listo",
            "comprometido": "Comprometido en pedidos", "a_producir": "Falta producir",
        })

        def resaltar_falta(row):
            return ["background-color: #ffd6d6" if row["Falta producir"] > 0 else ""] * len(row)

        st.dataframe(tabla_prod.style.apply(resaltar_falta, axis=1), use_container_width=True, hide_index=True)
        faltan = tabla_prod[tabla_prod["Falta producir"] > 0]
        if not faltan.empty:
            resumen_falta = ", ".join(f"{row['Sabor']}: {row['Falta producir']}" for _, row in faltan.iterrows())
            st.warning(f"⚠️ Necesitas producir: {resumen_falta}")

# =======================================================================================
# PÁGINA: VENTAS
# =======================================================================================
elif pagina == "🧾 Ventas":
    st.title("🧾 Seguimiento de ventas")

    inventario_df = obtener_inventario()
    editando = st.session_state.editando_venta_id
    sufijo_key = f"edit{editando}" if editando else "nuevo"

    # -----------------------------------------------------------------------------
    # SECCIÓN: crear o editar una venta (usa el mismo carrito para ambos casos)
    # -----------------------------------------------------------------------------
    if editando:
        st.subheader(f"✏️ Editando venta #{editando}")
        if st.button("Cancelar edición"):
            st.session_state.editando_venta_id = None
            st.session_state.carrito = []
            st.rerun()
        venta_editar = obtener_ventas().set_index("id").loc[editando]
        default_cliente = venta_editar["cliente"]
        default_lugar = venta_editar["lugar"]
        default_fecha_venta = pd.to_datetime(venta_editar["fecha_venta"]).date()
        default_fecha_entrega = pd.to_datetime(venta_editar["fecha_entrega"]).date()
        default_descuento = int(venta_editar["descuento"])
    else:
        st.subheader("➕ Registrar nueva venta")
        default_cliente, default_lugar = "", ""
        default_fecha_venta, default_fecha_entrega = date.today(), date.today()
        default_descuento = 0

    if inventario_df.empty:
        st.info("Primero agrega al menos un sabor en la pestaña 📦 Inventario.")
    else:
        st.markdown("**1. Cliente**")
        todas_ventas_df = obtener_ventas()
        clientes_existentes = sorted([c for c in todas_ventas_df["cliente"].dropna().unique().tolist() if c])
        opciones_cliente = ["➕ Cliente nuevo..."] + clientes_existentes
        idx_default = opciones_cliente.index(default_cliente) if default_cliente in opciones_cliente else 0
        cliente_sel = st.selectbox("Cliente", opciones_cliente, index=idx_default, key=f"cliente_sel_{sufijo_key}")
        if cliente_sel == "➕ Cliente nuevo...":
            valor_nuevo = default_cliente if default_cliente not in clientes_existentes else ""
            cliente = st.text_input("Nombre del nuevo cliente", value=valor_nuevo, key=f"cliente_nuevo_{sufijo_key}")
        else:
            cliente = cliente_sel

        st.markdown("**2. Agrega los productos de esta venta**")
        c1, c2, c3, c4 = st.columns([3, 1, 1.5, 1])
        sabor_sel = c1.selectbox("Sabor", SABORES_DISPONIBLES, key="sabor_carrito")
        cant_sel = c2.number_input("Cantidad", min_value=1, step=1, value=1, key="cant_carrito")

        fila_inv = inventario_df[inventario_df["sabor"] == sabor_sel]
        precio_sugerido = float(fila_inv["precio_venta"].values[0]) if not fila_inv.empty else 0.0
        # La key incluye el sabor para que, al cambiar de sabor, se refresque el precio
        # sugerido en vez de arrastrar el precio que hayas escrito para el sabor anterior.
        precio_manual = c3.number_input(
            "Precio unitario ($)", min_value=0, step=100, value=int(precio_sugerido),
            key=f"precio_carrito_{sabor_sel}",
        )
        if precio_sugerido == 0:
            st.caption(f"ℹ️ '{sabor_sel}' todavía no tiene precio cargado en Inventario. Puedes escribirlo aquí para esta venta, o cargarlo en Inventario para que quede guardado.")

        if c4.button("➕ Agregar", use_container_width=True):
            st.session_state.carrito.append({
                "sabor": sabor_sel, "cantidad": int(cant_sel),
                "precio_unitario": float(precio_manual), "subtotal": float(precio_manual) * cant_sel,
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
            st.info("Agrega al menos un producto al carrito.")

        st.markdown("**3. Datos de la venta y confirmación**")
        with st.form(f"form_confirmar_venta_{sufijo_key}"):
            c1, c2 = st.columns(2)
            lugar = c1.text_input("Lugar (feria, domicilio, retiro, etc.)", value=default_lugar)
            descuento = c2.number_input("Descuento ($)", min_value=0, step=100, value=default_descuento)
            c3, c4 = st.columns(2)
            fecha_venta = c3.date_input("Fecha de venta", value=default_fecha_venta)
            fecha_entrega = c4.date_input("Fecha de entrega", value=default_fecha_entrega)
            texto_boton = "Guardar cambios de la venta" if editando else "Confirmar venta"
            confirmar = st.form_submit_button(texto_boton)

            if confirmar:
                if not st.session_state.carrito:
                    st.error("Agrega al menos un producto al carrito.")
                elif not cliente.strip():
                    st.error("El nombre del cliente no puede estar vacío.")
                else:
                    if editando:
                        ok, msg = actualizar_venta_completa(
                            editando, cliente, lugar, fecha_venta, fecha_entrega, descuento, st.session_state.carrito
                        )
                        st.session_state.editando_venta_id = None
                    else:
                        ok, msg = True, registrar_venta_multiple(
                            cliente, lugar, fecha_venta, fecha_entrega, descuento, st.session_state.carrito
                        )[1]
                    st.session_state.carrito = []
                    st.success(msg) if ok else st.error(msg)
                    st.rerun()

    # -----------------------------------------------------------------------------
    # SECCIÓN: gestionar una venta abierta (estado, editar, eliminar)
    # -----------------------------------------------------------------------------
    st.divider()
    st.subheader("🔄 Gestionar una venta abierta")
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
            pago = c1.selectbox("Estado de pago", ["Pendiente", "Pagado"], index=["Pendiente", "Pagado"].index(fila_venta["estado_pago"]))
            entrega = c2.selectbox("Estado de entrega", ["Pendiente", "Entregado"], index=["Pendiente", "Entregado"].index(fila_venta["estado_entrega"]))
            guardar_venta = st.form_submit_button("Guardar estado de pago/entrega")
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

        with st.expander("⚙️ Otras acciones (editar o eliminar esta venta)"):
            colA, colB = st.columns(2)
            if colA.button("✏️ Editar esta venta", key="btn_editar_iniciar"):
                detalle_actual = obtener_detalle(venta_id)
                st.session_state.carrito = [
                    {"sabor": r.sabor, "cantidad": int(r.cantidad), "precio_unitario": float(r.precio_unitario), "subtotal": float(r.subtotal)}
                    for r in detalle_actual.itertuples()
                ]
                st.session_state.editando_venta_id = venta_id
                st.info("Sube al inicio de la página: ahí puedes modificar los productos y datos de esta venta.")
                st.rerun()

            if colB.button("🗑️ Eliminar esta venta", key="btn_eliminar_iniciar"):
                st.session_state.confirmar_eliminar_id = venta_id
                st.rerun()

            if st.session_state.confirmar_eliminar_id == venta_id:
                st.warning(f"¿Seguro que quieres eliminar la venta #{venta_id}? Esta acción no se puede deshacer.")
                cc1, cc2 = st.columns(2)
                if cc1.button("Sí, eliminar definitivamente", key="btn_confirmar_eliminar"):
                    ok, msg = eliminar_venta_abierta(venta_id)
                    st.session_state.confirmar_eliminar_id = None
                    st.success(msg) if ok else st.error(msg)
                    st.rerun()
                if cc2.button("Cancelar", key="btn_cancelar_eliminar"):
                    st.session_state.confirmar_eliminar_id = None
                    st.rerun()

        st.markdown("**Todas las ventas abiertas:**")
        st.dataframe(ventas_abiertas_df.drop(columns=["subtotal_bruto"], errors="ignore"), use_container_width=True, hide_index=True)

    # -----------------------------------------------------------------------------
    # SECCIÓN: historial y anulación de ventas cerradas
    # -----------------------------------------------------------------------------
    st.divider()
    st.subheader("📜 Historial de ventas cerradas")
    ventas_cerradas_df = obtener_ventas("Cerrada")
    if ventas_cerradas_df.empty:
        st.info("Todavía no hay ventas cerradas.")
    else:
        st.dataframe(ventas_cerradas_df.drop(columns=["subtotal_bruto"], errors="ignore"), use_container_width=True, hide_index=True)

        with st.expander("↩️ Anular una venta cerrada (repone el stock)"):
            opciones_anular = {f"#{int(r.id)} — {r.cliente} — {clp(r.total)}": int(r.id) for r in ventas_cerradas_df.itertuples()}
            sel_anular = st.selectbox("Selecciona la venta a anular", list(opciones_anular.keys()), key="sel_anular")
            venta_id_anular = opciones_anular[sel_anular]
            st.caption(
                "Esto marca la venta como 'Anulada', repone las unidades vendidas al stock, y la "
                "excluye de los ingresos y rankings. No se puede deshacer."
            )
            if st.button("Anular esta venta", key="btn_anular_iniciar"):
                st.session_state.confirmar_anular_id = venta_id_anular
                st.rerun()
            if st.session_state.confirmar_anular_id == venta_id_anular:
                st.warning(f"¿Confirmas anular la venta #{venta_id_anular}?")
                cc1, cc2 = st.columns(2)
                if cc1.button("Sí, anular definitivamente", key="btn_confirmar_anular"):
                    ok, msg = anular_venta_cerrada(venta_id_anular)
                    st.session_state.confirmar_anular_id = None
                    st.success(msg) if ok else st.error(msg)
                    st.rerun()
                if cc2.button("Cancelar", key="btn_cancelar_anular"):
                    st.session_state.confirmar_anular_id = None
                    st.rerun()

    ventas_anuladas_df = obtener_ventas("Anulada")
    if not ventas_anuladas_df.empty:
        with st.expander(f"🚫 Ventas anuladas ({len(ventas_anuladas_df)})"):
            st.dataframe(ventas_anuladas_df.drop(columns=["subtotal_bruto"], errors="ignore"), use_container_width=True, hide_index=True)
