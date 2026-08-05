"""
=====================================================================================
 CONTROL DE INVENTARIO Y VENTAS - MERMELADAS
=====================================================================================
Aplicación web hecha con Streamlit para llevar el inventario de sabores de mermelada
y el seguimiento de ventas, con cierre automático de la venta cuando queda Pagada y
Entregada (y hay stock suficiente para descontar).

IMPORTANTE SOBRE LOS DATOS (léelo antes de usar en producción):
Esta app guarda los datos en un archivo SQLite local (mermeladas.db). Cuando se aloja
gratis en Streamlit Community Cloud, ese archivo puede BORRARSE cada vez que la
plataforma reinicia el contenedor de la app (por ejemplo al editar el código, al
"despertar" tras un tiempo dormida, o por mantenimiento). Por eso la app incluye una
pestaña "Copia de seguridad": úsala para descargar un respaldo en Excel después de cada
sesión de trabajo, y para restaurarlo si alguna vez ves el inventario o las ventas en
blanco. Más detalles en la guía de despliegue que acompaña este archivo.
=====================================================================================
"""

import streamlit as st
import sqlite3
import pandas as pd
import io
from datetime import datetime

# -------------------------------------------------------------------------------------
# CONFIGURACIÓN GENERAL
# -------------------------------------------------------------------------------------
DB_PATH = "mermeladas.db"
UMBRAL_STOCK_BAJO_DEFAULT = 5

st.set_page_config(page_title="Control Mermeladas", page_icon="🍓", layout="wide")


# -------------------------------------------------------------------------------------
# BASE DE DATOS
# -------------------------------------------------------------------------------------
def get_connection():
    """Abre una nueva conexión a la base de datos SQLite."""
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    """Crea las tablas si todavía no existen. Se llama una vez al iniciar la app."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS inventario (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sabor TEXT UNIQUE NOT NULL,
            stock INTEGER NOT NULL DEFAULT 0,
            precio_unitario REAL NOT NULL DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ventas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            cliente TEXT NOT NULL,
            sabor TEXT NOT NULL,
            cantidad INTEGER NOT NULL,
            precio_unitario REAL NOT NULL,
            total REAL NOT NULL,
            estado_pago TEXT NOT NULL DEFAULT 'Pendiente',
            estado_entrega TEXT NOT NULL DEFAULT 'Pendiente',
            estado_venta TEXT NOT NULL DEFAULT 'Abierta'
        )
    """)
    conn.commit()
    conn.close()


init_db()


# -------------------------------------------------------------------------------------
# FUNCIONES - INVENTARIO
# -------------------------------------------------------------------------------------
def obtener_inventario():
    conn = get_connection()
    df = pd.read_sql_query("SELECT id, sabor, stock, precio_unitario FROM inventario ORDER BY sabor", conn)
    conn.close()
    return df


def agregar_sabor(sabor, stock, precio):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO inventario (sabor, stock, precio_unitario) VALUES (?, ?, ?)",
            (sabor.strip(), int(stock), float(precio)),
        )
        conn.commit()
        return True, f"Sabor '{sabor}' agregado correctamente."
    except sqlite3.IntegrityError:
        return False, f"El sabor '{sabor}' ya existe en el inventario."
    finally:
        conn.close()


def actualizar_sabor(sabor_id, nuevo_stock, nuevo_precio):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE inventario SET stock = ?, precio_unitario = ? WHERE id = ?",
        (int(nuevo_stock), float(nuevo_precio), sabor_id),
    )
    conn.commit()
    conn.close()


# -------------------------------------------------------------------------------------
# FUNCIONES - VENTAS
# -------------------------------------------------------------------------------------
def obtener_ventas(estado=None):
    conn = get_connection()
    if estado:
        df = pd.read_sql_query(
            "SELECT * FROM ventas WHERE estado_venta = ? ORDER BY id DESC", conn, params=(estado,)
        )
    else:
        df = pd.read_sql_query("SELECT * FROM ventas ORDER BY id DESC", conn)
    conn.close()
    return df


def registrar_venta(cliente, sabor, cantidad):
    """Crea una nueva venta en estado Abierta / Pendiente / Pendiente."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT precio_unitario, stock FROM inventario WHERE sabor = ?", (sabor,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        return False, "El sabor seleccionado no existe en el inventario."

    precio_unitario, stock_actual = row
    total = precio_unitario * cantidad
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M")

    cur.execute(
        """INSERT INTO ventas
           (fecha, cliente, sabor, cantidad, precio_unitario, total, estado_pago, estado_entrega, estado_venta)
           VALUES (?, ?, ?, ?, ?, ?, 'Pendiente', 'Pendiente', 'Abierta')""",
        (fecha, cliente.strip(), sabor, int(cantidad), precio_unitario, total),
    )
    conn.commit()
    conn.close()

    if cantidad > stock_actual:
        return True, (
            f"Venta registrada. Atención: el stock actual de '{sabor}' ({stock_actual}) es menor a la "
            f"cantidad vendida ({cantidad}). Deberás reponer stock antes de poder cerrar esta venta."
        )
    return True, "Venta registrada correctamente."


def actualizar_estado_venta(venta_id, nuevo_pago, nueva_entrega):
    """
    Actualiza el estado de pago/entrega de una venta.

    Si al hacerlo se cumplen las 3 condiciones del negocio:
      a) hay stock suficiente
      b) estado_entrega == 'Entregado'
      c) estado_pago == 'Pagado'
    ...entonces la venta se cierra automáticamente y se descuenta el stock.

    Si falta stock, se guardan los estados elegidos pero la venta NO se cierra,
    y se devuelve una advertencia para que el usuario ajuste el inventario.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT sabor, cantidad, estado_venta FROM ventas WHERE id = ?", (venta_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        return "error", "Venta no encontrada."

    sabor, cantidad, estado_venta = row
    if estado_venta == "Cerrada":
        conn.close()
        return "error", "Esta venta ya está cerrada y no se puede modificar."

    cur.execute(
        "UPDATE ventas SET estado_pago = ?, estado_entrega = ? WHERE id = ?",
        (nuevo_pago, nueva_entrega, venta_id),
    )

    if nuevo_pago == "Pagado" and nueva_entrega == "Entregado":
        cur.execute("SELECT stock FROM inventario WHERE sabor = ?", (sabor,))
        stock_row = cur.fetchone()
        stock_disponible = stock_row[0] if stock_row else 0

        if stock_disponible >= cantidad:
            cur.execute("UPDATE inventario SET stock = stock - ? WHERE sabor = ?", (cantidad, sabor))
            cur.execute("UPDATE ventas SET estado_venta = 'Cerrada' WHERE id = ?", (venta_id,))
            conn.commit()
            conn.close()
            return "cerrada", f"Venta #{venta_id} cerrada. Se descontaron {cantidad} unidades de '{sabor}' del inventario."
        else:
            conn.commit()
            conn.close()
            return "advertencia", (
                f"No se puede cerrar la venta #{venta_id}: stock insuficiente de '{sabor}' "
                f"(disponible: {stock_disponible}, requerido: {cantidad}). "
                f"Repón stock en la pestaña Inventario e inténtalo de nuevo."
            )
    else:
        conn.commit()
        conn.close()
        return "guardado", "Cambios guardados."


# -------------------------------------------------------------------------------------
# FUNCIONES - COPIA DE SEGURIDAD
# -------------------------------------------------------------------------------------
def generar_backup_excel():
    conn = get_connection()
    inventario_df = pd.read_sql_query("SELECT * FROM inventario", conn)
    ventas_df = pd.read_sql_query("SELECT * FROM ventas", conn)
    conn.close()

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        inventario_df.to_excel(writer, sheet_name="inventario", index=False)
        ventas_df.to_excel(writer, sheet_name="ventas", index=False)
    buffer.seek(0)
    return buffer


def restaurar_backup_excel(archivo):
    inventario_df = pd.read_excel(archivo, sheet_name="inventario")
    ventas_df = pd.read_excel(archivo, sheet_name="ventas")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM inventario")
    cur.execute("DELETE FROM ventas")
    conn.commit()
    inventario_df.to_sql("inventario", conn, if_exists="append", index=False)
    ventas_df.to_sql("ventas", conn, if_exists="append", index=False)
    conn.close()


# -------------------------------------------------------------------------------------
# UTILIDADES DE FORMATO
# -------------------------------------------------------------------------------------
def clp(valor):
    """Formatea un número como precio en pesos chilenos, ej: $12.500"""
    return "$" + f"{valor:,.0f}".replace(",", ".")


# -------------------------------------------------------------------------------------
# INTERFAZ - BARRA LATERAL
# -------------------------------------------------------------------------------------
st.sidebar.title("🍓 Control Mermeladas")
pagina = st.sidebar.radio(
    "Ir a:",
    ["📊 Resumen", "📦 Inventario", "🧾 Ventas", "💾 Copia de seguridad"],
)

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
    ingresos = ventas_cerradas_df["total"].sum() if not ventas_cerradas_df.empty else 0
    col4.metric("Ingresos cobrados", clp(ingresos))

    st.divider()
    st.subheader("Ventas abiertas pendientes de gestionar")
    if ventas_abiertas_df.empty:
        st.info("No hay ventas abiertas por el momento.")
    else:
        st.dataframe(ventas_abiertas_df, use_container_width=True, hide_index=True)

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
            c1, c2, c3 = st.columns(3)
            nuevo_sabor = c1.text_input("Sabor / variedad")
            nuevo_stock = c2.number_input("Stock inicial", min_value=0, step=1, value=0)
            nuevo_precio = c3.number_input("Precio unitario ($)", min_value=0, step=100, value=0)
            enviado = st.form_submit_button("Agregar sabor")
            if enviado:
                if not nuevo_sabor.strip():
                    st.error("El nombre del sabor no puede estar vacío.")
                else:
                    ok, msg = agregar_sabor(nuevo_sabor, nuevo_stock, nuevo_precio)
                    st.success(msg) if ok else st.error(msg)
                    st.rerun()

    st.subheader("Stock actual")
    inventario_df = obtener_inventario()

    if inventario_df.empty:
        st.info("Todavía no hay sabores registrados. Agrega el primero arriba. ⬆️")
    else:
        tabla = inventario_df.rename(
            columns={"sabor": "Sabor", "stock": "Stock", "precio_unitario": "Precio unitario"}
        )[["Sabor", "Stock", "Precio unitario"]].copy()
        tabla["Precio unitario"] = tabla["Precio unitario"].apply(clp)

        def resaltar_stock_bajo(row):
            es_bajo = inventario_df.loc[inventario_df["sabor"] == row["Sabor"], "stock"].values[0] < umbral
            color = "background-color: #ffd6d6" if es_bajo else ""
            return [color] * len(row)

        st.dataframe(
            tabla.style.apply(resaltar_stock_bajo, axis=1),
            use_container_width=True, hide_index=True,
        )

        bajo_stock = inventario_df[inventario_df["stock"] < umbral]
        if not bajo_stock.empty:
            nombres = ", ".join(bajo_stock["sabor"].tolist())
            st.warning(f"⚠️ Sabores con stock bajo: {nombres}")

        st.subheader("✏️ Editar stock o precio de un sabor")
        opciones = {f"{r.sabor} (stock actual: {r.stock})": r.id for r in inventario_df.itertuples()}
        seleccion = st.selectbox("Selecciona un sabor", list(opciones.keys()))
        sabor_id = opciones[seleccion]
        fila = inventario_df[inventario_df["id"] == sabor_id].iloc[0]

        with st.form("form_editar_sabor"):
            c1, c2 = st.columns(2)
            stock_editado = c1.number_input("Nuevo stock", min_value=0, step=1, value=int(fila["stock"]))
            precio_editado = c2.number_input(
                "Nuevo precio unitario ($)", min_value=0, step=100, value=int(fila["precio_unitario"])
            )
            guardar = st.form_submit_button("Guardar cambios")
            if guardar:
                actualizar_sabor(sabor_id, stock_editado, precio_editado)
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
        with st.form("form_nueva_venta", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            cliente = c1.text_input("Cliente")
            sabor_venta = c2.selectbox("Sabor de mermelada", inventario_df["sabor"].tolist())
            cantidad_venta = c3.number_input("Cantidad", min_value=1, step=1, value=1)
            registrar = st.form_submit_button("Registrar venta")
            if registrar:
                if not cliente.strip():
                    st.error("El nombre del cliente no puede estar vacío.")
                else:
                    ok, msg = registrar_venta(cliente, sabor_venta, cantidad_venta)
                    st.success(msg) if ok else st.error(msg)
                    st.rerun()

    st.divider()
    st.subheader("🔄 Actualizar estado de una venta abierta")
    ventas_abiertas_df = obtener_ventas("Abierta")

    if ventas_abiertas_df.empty:
        st.info("No hay ventas abiertas en este momento.")
    else:
        opciones_venta = {
            f"#{r.id} — {r.cliente} — {r.sabor} x{r.cantidad} — {clp(r.total)}": r.id
            for r in ventas_abiertas_df.itertuples()
        }
        seleccion_venta = st.selectbox("Selecciona una venta", list(opciones_venta.keys()))
        venta_id = opciones_venta[seleccion_venta]
        fila_venta = ventas_abiertas_df[ventas_abiertas_df["id"] == venta_id].iloc[0]

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
        st.dataframe(ventas_abiertas_df, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("📜 Historial de ventas cerradas")
    ventas_cerradas_df = obtener_ventas("Cerrada")
    if ventas_cerradas_df.empty:
        st.info("Todavía no hay ventas cerradas.")
    else:
        st.dataframe(ventas_cerradas_df, use_container_width=True, hide_index=True)

# =======================================================================================
# PÁGINA: COPIA DE SEGURIDAD
# =======================================================================================
elif pagina == "💾 Copia de seguridad":
    st.title("💾 Copia de seguridad")
    st.warning(
        "En el hosting gratuito, los datos guardados localmente pueden borrarse cuando la "
        "app se reinicia (por ejemplo al editar el código o tras estar dormida por inactividad). "
        "Descarga un respaldo después de cada sesión de trabajo y guárdalo en tu computador o "
        "en Google Drive / correo."
    )

    st.subheader("⬇️ Descargar respaldo actual")
    buffer = generar_backup_excel()
    st.download_button(
        "Descargar backup en Excel",
        data=buffer,
        file_name=f"backup_mermeladas_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.subheader("⬆️ Restaurar desde un respaldo")
    st.caption("⚠️ Esto reemplaza TODOS los datos actuales de inventario y ventas por los del archivo subido.")
    archivo_subido = st.file_uploader("Sube un archivo de backup (.xlsx)", type=["xlsx"])
    if archivo_subido is not None:
        if st.button("Confirmar y restaurar"):
            try:
                restaurar_backup_excel(archivo_subido)
                st.success("Datos restaurados correctamente.")
                st.rerun()
            except Exception as e:
                st.error(f"No se pudo restaurar el archivo: {e}")
