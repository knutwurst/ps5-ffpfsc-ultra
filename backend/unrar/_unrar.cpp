#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <wchar.h>
#include <stdint.h>
#include <vector>
#include <string>
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
    if (msg == UCM_CHANGEVOLUME || msg == UCM_CHANGEVOLUMEW) {
        // A part of the set is missing (a drive that dropped out or went to sleep
        // during the extraction): give up, UnRAR then fails with ERAR_EOPEN. Any
        // other answer to RAR_VOL_ASK makes UnRAR wait for the part, forever.
        return p2 == RAR_VOL_ASK ? -1 : 1;
    }
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

// Extract only the members named in *names* (archive paths, '/'-separated), each to
// the matching full path in *dest_paths*; every other member is skipped. A solid
// archive is refused unless *allow_solid*: there, skipping still decompresses all the
// data before a member, which for a game set means most of the archive. Stops once
// every name was found. Returns the number of members written.
static PyObject* py_extract_names(PyObject* self, PyObject* args, PyObject* kwargs) {
    static const char* kwlist[] = {"archive_path", "names", "dest_paths", "password", "allow_solid", NULL};
    const char* archive_path = NULL;
    PyObject* names_obj = NULL;
    PyObject* dests_obj = NULL;
    const char* password = NULL;
    int allow_solid = 0;
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "sOO|zp", (char**)kwlist,
                                     &archive_path, &names_obj, &dests_obj, &password, &allow_solid))
        return NULL;
    PyObject* names_seq = PySequence_Fast(names_obj, "names must be a sequence");
    if (!names_seq) return NULL;
    PyObject* dests_seq = PySequence_Fast(dests_obj, "dest_paths must be a sequence");
    if (!dests_seq) { Py_DECREF(names_seq); return NULL; }
    Py_ssize_t n = PySequence_Fast_GET_SIZE(names_seq);
    if (n != PySequence_Fast_GET_SIZE(dests_seq)) {
        Py_DECREF(names_seq); Py_DECREF(dests_seq);
        PyErr_SetString(PyExc_ValueError, "names and dest_paths differ in length");
        return NULL;
    }
    std::vector<std::wstring> names, dests;
    for (Py_ssize_t i = 0; i < n; i++) {
        wchar_t* a = PyUnicode_AsWideCharString(PySequence_Fast_GET_ITEM(names_seq, i), NULL);
        wchar_t* b = a ? PyUnicode_AsWideCharString(PySequence_Fast_GET_ITEM(dests_seq, i), NULL) : NULL;
        if (!a || !b) {
            if (a) PyMem_Free(a);
            Py_DECREF(names_seq); Py_DECREF(dests_seq);
            return NULL;
        }
        std::wstring w(a);
        for (auto& ch : w) if (ch == L'\\') ch = L'/';
        names.push_back(w);
        dests.push_back(std::wstring(b));
        PyMem_Free(a); PyMem_Free(b);
    }
    Py_DECREF(names_seq); Py_DECREF(dests_seq);

    RAROpenArchiveDataEx arcData = {};
    arcData.ArcName = const_cast<char*>(archive_path);
    arcData.OpenMode = RAR_OM_EXTRACT;
    HANDLE hArc; int openRes;
    Py_BEGIN_ALLOW_THREADS
    hArc = RAROpenArchiveEx(&arcData);
    openRes = arcData.OpenResult;
    Py_END_ALLOW_THREADS
    if (!hArc || openRes != ERAR_SUCCESS) {
        PyErr_Format(UnrarError, "Failed to open archive (error %d)", openRes);
        return NULL;
    }
    if ((arcData.Flags & ROADF_SOLID) && !allow_solid) {
        RARCloseArchive(hArc);
        PyErr_SetString(UnrarError, "solid archive: a member cannot be read without decompressing what precedes it");
        return NULL;
    }
    if (password && password[0])
        RARSetPassword(hArc, const_cast<char*>(password));
    ProgressCtx ctx = { NULL, NULL, 0, 0, 4LL * 1024 * 1024, 0 };   // for the volume answers only
    RARSetCallback(hArc, ExtractCallback, reinterpret_cast<LPARAM>(&ctx));

    long found = 0;
    int result;
    RARHeaderDataEx header = {};
    for (;;) {
        Py_BEGIN_ALLOW_THREADS
        result = RARReadHeaderEx(hArc, &header);
        Py_END_ALLOW_THREADS
        if (result != ERAR_SUCCESS)
            break;
        std::wstring name(header.FileNameW);
        for (auto& ch : name) if (ch == L'\\') ch = L'/';
        Py_ssize_t hit = -1;
        for (Py_ssize_t i = 0; i < n; i++)
            if (names[i] == name) { hit = i; break; }
        int pres;
        if (hit >= 0) {
            Py_BEGIN_ALLOW_THREADS
            pres = RARProcessFileW(hArc, RAR_EXTRACT, NULL, const_cast<wchar_t*>(dests[hit].c_str()));
            Py_END_ALLOW_THREADS
        } else {
            Py_BEGIN_ALLOW_THREADS
            pres = RARProcessFile(hArc, RAR_SKIP, NULL, NULL);
            Py_END_ALLOW_THREADS
        }
        if (pres != ERAR_SUCCESS) {
            RARCloseArchive(hArc);
            if (pres == ERAR_MISSING_PASSWORD || pres == ERAR_BAD_PASSWORD)
                PyErr_SetString(PyExc_PermissionError, "Password required or incorrect");
            else
                PyErr_Format(UnrarError, "Extraction failed for %ls (error %d)", header.FileNameW, pres);
            return NULL;
        }
        if (hit >= 0 && ++found == (long)n)
            break;
    }
    RARCloseArchive(hArc);
    if (found < (long)n && result != ERAR_END_ARCHIVE && result != ERAR_SUCCESS) {
        PyErr_Format(UnrarError, "Read header failed (error %d)", result);
        return NULL;
    }
    return PyLong_FromLong(found);
}

static PyMethodDef UnrarMethods[] = {
    {"list_files", (PyCFunction)py_list_files, METH_VARARGS | METH_KEYWORDS,
     "list_files(archive_path, password=None) -> list[dict]\n\nReturn list of file info dicts from a RAR archive."},
    {"extract_all", (PyCFunction)py_extract_all, METH_VARARGS | METH_KEYWORDS,
     "extract_all(archive_path, dest_path, password=None) -> int\n\nExtract all files from a RAR archive. Returns count of extracted files."},
    {"extract_names", (PyCFunction)py_extract_names, METH_VARARGS | METH_KEYWORDS,
     "extract_names(archive_path, names, dest_paths, password=None, allow_solid=False) -> int\n\n"
     "Extract only the named members, each to its own path; refuses a solid archive unless allowed."},
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
