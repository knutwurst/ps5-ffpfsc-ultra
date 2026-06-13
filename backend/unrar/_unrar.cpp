#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <wchar.h>
#include <stdint.h>
#include "src/rar.hpp"

static PyObject* UnrarError;

static int is_safe_filename(const wchar_t* name) {
    if (!name || !name[0]) return 0;
    if (name[0] == L'/' || name[0] == L'\\') return 0;
    const wchar_t* p = name;
    while (*p) {
        if (p[0] == L'.' && p[1] == L'.') {
            if (p[2] == L'/' || p[2] == L'\\' || p[2] == L'\0') return 0;
        }
        if ((p[0] == L'/' || p[0] == L'\\') && p[1] == L'.' && p[2] == L'.') {
            if (p[3] == L'/' || p[3] == L'\\' || p[3] == L'\0') return 0;
        }
        p++;
    }
    return 1;
}

// ── Progress reporting ──────────────────────────────────────────────────────
// UnRAR fires UCM_PROCESSDATA with chunk sizes while extracting. We accumulate
// the byte count and, throttled, call back into Python — re-acquiring the GIL,
// which is released around the heavy RAR calls so the host GUI stays responsive.
struct ProgressCtx {
    PyObject* cb;            // Python callable(total_bytes_done) or NULL
    PyObject* cancel_cb;     // Python callable() -> truthy to abort, or NULL
    long long done;          // cumulative bytes extracted
    long long last_report;   // bytes at last Python notification
    long long step;          // notify/poll at most every `step` bytes
    int cancelled;           // set to 1 once cancel_cb requested an abort
};

static int CALLBACK ExtractCallback(UINT msg, LPARAM userData, LPARAM p1, LPARAM p2) {
    (void)p1;
    if (msg == UCM_PROCESSDATA) {
        ProgressCtx* ctx = reinterpret_cast<ProgressCtx*>(userData);
        if (ctx) {
            ctx->done += (long long)p2;
            // Throttle BOTH the progress report and the cancel poll to once per
            // `step` bytes so we only take the GIL ~every few MB, not per chunk.
            if ((ctx->cb || ctx->cancel_cb) && ctx->done - ctx->last_report >= ctx->step) {
                ctx->last_report = ctx->done;
                PyGILState_STATE gil = PyGILState_Ensure();
                if (ctx->cb) {
                    PyObject* r = PyObject_CallFunction(ctx->cb, "L", ctx->done);
                    if (r) Py_DECREF(r);
                    else PyErr_Clear();   // a progress hiccup must never abort extraction
                }
                if (ctx->cancel_cb) {
                    PyObject* r = PyObject_CallObject(ctx->cancel_cb, NULL);
                    if (r) { if (PyObject_IsTrue(r) == 1) ctx->cancelled = 1; Py_DECREF(r); }
                    else PyErr_Clear();
                }
                PyGILState_Release(gil);
            }
            if (ctx->cancelled)
                return -1;   // tell UnRAR to abort processing
        }
    }
    return 0;  // continue
}

// Set dict[key] = value where `value` is a NEW reference (e.g. straight from
// PyUnicode_FromWideChar / PyLong_*). Drops our reference so it does not leak,
// and treats a NULL value as failure. Returns 0 on success, -1 on failure.
static int dict_set_new(PyObject* d, const char* key, PyObject* value) {
    if (!value)
        return -1;
    int rc = PyDict_SetItemString(d, key, value);
    Py_DECREF(value);
    return rc;
}

static PyObject* py_list_files(PyObject* self, PyObject* args, PyObject* kwargs) {
    static const char* kwlist[] = {"archive_path", "password", NULL};
    const char* archive_path = NULL;
    const char* password = NULL;
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "s|z", (char**)kwlist,
                                     &archive_path, &password))
        return NULL;

    RAROpenArchiveDataEx arcData = {};
    arcData.ArcName = const_cast<char*>(archive_path);
    arcData.OpenMode = RAR_OM_LIST;

    HANDLE hArc; int openRes;
    Py_BEGIN_ALLOW_THREADS          // opening probes the (first) volume — release GIL
    hArc = RAROpenArchiveEx(&arcData);
    openRes = arcData.OpenResult;
    Py_END_ALLOW_THREADS
    if (!hArc || openRes != ERAR_SUCCESS) {
        PyErr_Format(UnrarError, "Failed to open archive (error %d)", openRes);
        return NULL;
    }

    if (password && password[0]) {
        RARSetPassword(hArc, const_cast<char*>(password));
    }

    PyObject* result = PyList_New(0);
    if (!result) { RARCloseArchive(hArc); return NULL; }
    RARHeaderDataEx header = {};
    int res;

    for (;;) {
        Py_BEGIN_ALLOW_THREADS
        res = RARReadHeaderEx(hArc, &header);
        Py_END_ALLOW_THREADS
        if (res != ERAR_SUCCESS) break;

        PyObject* info = PyDict_New();
        int bad = (info == NULL);
        if (!bad) {
            bad |= dict_set_new(info, "filename", PyUnicode_FromWideChar(header.FileNameW, -1));
            bad |= dict_set_new(info, "file_size",
                PyLong_FromUnsignedLongLong(((uint64_t)header.UnpSizeHigh << 32) | header.UnpSize));
            bad |= dict_set_new(info, "compress_size",
                PyLong_FromUnsignedLongLong(((uint64_t)header.PackSizeHigh << 32) | header.PackSize));
            bad |= dict_set_new(info, "is_directory",
                PyBool_FromLong((header.Flags & RHDF_DIRECTORY) ? 1 : 0));
            if (!bad)
                bad = PyList_Append(result, info);
        }
        Py_XDECREF(info);
        if (bad) {
            Py_DECREF(result);
            RARCloseArchive(hArc);
            if (!PyErr_Occurred())
                PyErr_SetString(UnrarError, "Failed to build archive file list");
            return NULL;
        }

        Py_BEGIN_ALLOW_THREADS
        RARProcessFile(hArc, RAR_SKIP, NULL, NULL);
        Py_END_ALLOW_THREADS
    }

    RARCloseArchive(hArc);

    if (res != ERAR_END_ARCHIVE) {
        Py_DECREF(result);
        PyErr_Format(UnrarError, "Read header failed (error %d)", res);
        return NULL;
    }
    return result;
}

static PyObject* py_extract_all(PyObject* self, PyObject* args, PyObject* kwargs) {
    static const char* kwlist[] = {"archive_path", "dest_path", "password",
                                   "progress_callback", "cancel_callback", NULL};
    const char* archive_path = NULL;
    const char* dest_path = NULL;
    const char* password = NULL;
    PyObject* progress_cb = NULL;
    PyObject* cancel_cb = NULL;

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "ss|zOO", (char**)kwlist,
                                     &archive_path, &dest_path, &password,
                                     &progress_cb, &cancel_cb))
        return NULL;
    if (progress_cb == Py_None) progress_cb = NULL;
    if (cancel_cb == Py_None) cancel_cb = NULL;
    if (progress_cb && !PyCallable_Check(progress_cb)) {
        PyErr_SetString(PyExc_TypeError, "progress_callback must be callable or None");
        return NULL;
    }
    if (cancel_cb && !PyCallable_Check(cancel_cb)) {
        PyErr_SetString(PyExc_TypeError, "cancel_callback must be callable or None");
        return NULL;
    }

    RAROpenArchiveDataEx arcData = {};
    arcData.ArcName = const_cast<char*>(archive_path);
    arcData.OpenMode = RAR_OM_EXTRACT;

    HANDLE hArc; int openRes;
    Py_BEGIN_ALLOW_THREADS          // opening probes the (first) volume — release GIL
    hArc = RAROpenArchiveEx(&arcData);
    openRes = arcData.OpenResult;
    Py_END_ALLOW_THREADS
    if (!hArc || openRes != ERAR_SUCCESS) {
        PyErr_Format(UnrarError, "Failed to open archive (error %d)", openRes);
        return NULL;
    }

    if (password && password[0]) {
        RARSetPassword(hArc, const_cast<char*>(password));
    }

    // Report progress / poll for cancel every ~4 MB to keep the GIL-take rate low.
    ProgressCtx ctx = { progress_cb, cancel_cb, 0, 0, 4LL * 1024 * 1024, 0 };
    RARSetCallback(hArc, ExtractCallback, reinterpret_cast<LPARAM>(&ctx));

    int count = 0;
    int result;
    RARHeaderDataEx header = {};

    for (;;) {
        // RARReadHeaderEx / RARProcessFile do the heavy I/O + decompression.
        // Release the GIL around them so the host application's UI thread runs.
        Py_BEGIN_ALLOW_THREADS
        result = RARReadHeaderEx(hArc, &header);
        Py_END_ALLOW_THREADS
        if (result != ERAR_SUCCESS)
            break;

        if (!is_safe_filename(header.FileNameW)) {
            RARCloseArchive(hArc);
            PyErr_Format(UnrarError, "Unsafe path in archive: %ls", header.FileNameW);
            return NULL;
        }

        int pres;
        Py_BEGIN_ALLOW_THREADS
        pres = RARProcessFile(hArc, RAR_EXTRACT, const_cast<char*>(dest_path), NULL);
        Py_END_ALLOW_THREADS

        if (pres != ERAR_SUCCESS) {
            RARCloseArchive(hArc);
            if (ctx.cancelled) {
                PyErr_SetString(UnrarError, "Extraction cancelled by user");
            } else if (pres == ERAR_MISSING_PASSWORD || pres == ERAR_BAD_PASSWORD) {
                PyErr_SetString(PyExc_PermissionError, "Password required or incorrect");
            } else {
                PyErr_Format(UnrarError, "Extraction failed for %ls (error %d)", header.FileNameW, pres);
            }
            return NULL;
        }
        count++;
    }

    RARCloseArchive(hArc);

    if (result != ERAR_END_ARCHIVE) {
        PyErr_Format(UnrarError, "Read header failed (error %d)", result);
        return NULL;
    }
    return PyLong_FromLong(count);
}

static PyMethodDef UnrarMethods[] = {
    {"list_files", (PyCFunction)py_list_files, METH_VARARGS | METH_KEYWORDS,
     "list_files(archive_path, password=None) -> list[dict]\n\nReturn list of file info dicts from a RAR archive."},
    {"extract_all", (PyCFunction)py_extract_all, METH_VARARGS | METH_KEYWORDS,
     "extract_all(archive_path, dest_path, password=None) -> int\n\nExtract all files from a RAR archive. Returns count of extracted files."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef unrar_module = {
    PyModuleDef_HEAD_INIT,
    "_unrar",
    "Python bindings for the UnRAR library",
    -1,
    UnrarMethods
};

PyMODINIT_FUNC PyInit__unrar(void) {
    PyObject* m = PyModule_Create(&unrar_module);
    if (m == NULL)
        return NULL;
    UnrarError = PyErr_NewException("unrar._unrar.UnrarError", NULL, NULL);
    Py_XINCREF(UnrarError);
    if (PyModule_AddObject(m, "UnrarError", UnrarError) < 0) {
        Py_XDECREF(UnrarError);
        Py_CLEAR(UnrarError);
        Py_DECREF(m);
        return NULL;
    }
    return m;
}
