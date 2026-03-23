from fastapi import FastAPI, HTTPException, Body
from typing import Dict, Any, List
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# Cargamos las variables de entorno desde el archivo .env
load_dotenv()

app = FastAPI(
    title="API - Base de Datos Dinámica en Supabase",
    description="Tu API para crear tablas e insertar datos al vuelo, respaldada por PostgreSQL en la nube.",
    version="3.0.0"
)

# Permitir CORS para que cualquier frontend remoto conecte con la API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# La conexión que te da Supabase en su panel (Settings -> Database -> Connection string -> URI)
DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="Falta configurar DATABASE_URL en el archivo .env o en las variables del servidor.")
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo conectar a Supabase: {str(e)}")

# ==========================================
# RUTAS PARA CREAR/ADMINISTRAR TABLAS (PostgreSQL)
# ==========================================

@app.post("/api/tablas/{nombre_tabla}", summary="Crear una nueva tabla en Supabase")
def crear_tabla(nombre_tabla: str, columnas: Dict[str, str] = Body(..., example={"nombre": "TEXT", "edad": "INTEGER"})):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # En Postgres usamos SERIAL para que el ID sea autoincremental
    Cols_def = ["id SERIAL PRIMARY KEY"]
    for nombre_col, tipo_col in columnas.items():
        # Validamos tipos de Postgres
        tipo = tipo_col.upper()
        if tipo not in ["TEXT", "VARCHAR", "INTEGER", "BOOLEAN", "REAL", "FLOAT", "DATE", "JSONB"]:
            tipo = "TEXT"
        Cols_def.append(f"{nombre_col} {tipo}")
        
    query = f"CREATE TABLE IF NOT EXISTS {nombre_tabla} ({', '.join(Cols_def)})"
    
    try:
        cursor.execute(query)
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Error creando tabla: {str(e)}")
        
    conn.close()
    return {"mensaje": f"Tabla '{nombre_tabla}' construida en tu proyecto de Supabase.", "columnas": columnas}

@app.get("/api/tablas/", summary="Ver la lista de todas las tablas de Supabase")
def listar_tablas():
    conn = get_db_connection()
    cursor = conn.cursor()
    # Consulta estándar en Postgres para ver tablas públicas
    cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    tablas = [row[0] for row in cursor.fetchall()]
    conn.close()
    return {"tablas_existentes": tablas}

@app.delete("/api/tablas/{nombre_tabla}", summary="Borrar una tabla en Supabase")
def borrar_tabla(nombre_tabla: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # CASCADE borra también todo lo que dependa de esta tabla
        cursor.execute(f"DROP TABLE IF EXISTS {nombre_tabla} CASCADE")
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=str(e))
    conn.close()
    return {"mensaje": f"La tabla '{nombre_tabla}' ha sido eliminada de Supabase."}

# ==============================================
# RUTAS PARA INSERTAR/LEER DATOS (PostgreSQL)
# ==============================================

@app.post("/api/datos/{nombre_tabla}", summary="Insertar datos en Supabase")
def insertar_dato(nombre_tabla: str, datos: Dict[str, Any] = Body(..., example={"nombre": "Juan", "edad": 25})):
    conn = get_db_connection()
    # Usamos RealDictCursor para que Postgres nos devuelva un diccionario fácil de leer
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    nombres_columnas = ", ".join(datos.keys())
    # Postgres usa %s en vez de ? para evitar Inyección SQL
    placeholders = ", ".join(["%s" for _ in datos])
    valores = tuple(datos.values())
    
    # RETURNING id nos devuelve el ID nuevo generado
    query = f"INSERT INTO {nombre_tabla} ({nombres_columnas}) VALUES ({placeholders}) RETURNING id"
    
    try:
        cursor.execute(query, valores)
        conn.commit()
        last_id = cursor.fetchone()['id']
    except psycopg2.errors.UndefinedTable:
        conn.close()
        raise HTTPException(status_code=404, detail="La tabla no existe en Supabase.")
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=str(e))
        
    conn.close()
    datos["id"] = last_id
    return {"mensaje": "Dato guardado con éxito", "dato_guardado": datos}

@app.get("/api/datos/{nombre_tabla}", summary="Leer datos de Supabase")
def leer_datos(nombre_tabla: str):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute(f"SELECT * FROM {nombre_tabla} ORDER BY id ASC")
        filas = cursor.fetchall()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=404, detail=f"No se pudo leer la tabla. Error: {str(e)}")
    conn.close()
    return filas

@app.put("/api/datos/{nombre_tabla}/{id}", summary="Actualizar Dato en Supabase")
def actualizar_dato(nombre_tabla: str, id: int, datos: Dict[str, Any] = Body(...)):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    set_clause = ", ".join([f"{k} = %s" for k in datos.keys()])
    valores = tuple(datos.values()) + (id,)
    
    query = f"UPDATE {nombre_tabla} SET {set_clause} WHERE id = %s RETURNING id"
    
    try:
        cursor.execute(query, valores)
        conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Dato no encontrado.")
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=str(e))
        
    conn.close()
    datos["id"] = id
    return {"mensaje": "Actualización exitosa", "dato_modificado": datos}

@app.delete("/api/datos/{nombre_tabla}/{id}", summary="Borrar registro específico")
def borrar_dato(nombre_tabla: str, id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"DELETE FROM {nombre_tabla} WHERE id = %s", (id,))
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=str(e))
    conn.close()
    return {"mensaje": f"El registro ID:{id} ha sido borrado de Supabase exitosamente."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
