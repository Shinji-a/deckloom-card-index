package dev.deckloom.imagelab

import android.content.Intent
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.ImageView
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.GridLayoutManager
import androidx.recyclerview.widget.RecyclerView
import java.io.File
import java.util.zip.ZipInputStream

class MainActivity : AppCompatActivity() {
    private lateinit var recycler: RecyclerView
    private lateinit var status: TextView
    private val adapter = CardAdapter()
    private var columns = 2

    private val picker = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) importZip(uri)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        recycler = findViewById(R.id.list)
        status = findViewById(R.id.status)
        recycler.adapter = adapter
        setColumns(2)
        findViewById<Button>(R.id.oneColumn).setOnClickListener { setColumns(1) }
        findViewById<Button>(R.id.twoColumn).setOnClickListener { setColumns(2) }

        val existing = filesDir.resolve("cards").listFiles()?.filter { it.extension.equals("webp", true) }?.sortedBy { it.name }.orEmpty()
        if (existing.isNotEmpty()) show(existing) else picker.launch(arrayOf("application/zip","application/octet-stream"))
    }

    private fun setColumns(value: Int) {
        columns = value
        recycler.layoutManager = GridLayoutManager(this, columns)
        recycler.scrollToPosition(0)
    }

    private fun importZip(uri: Uri) {
        status.text = "展開中…"
        val dir = filesDir.resolve("cards").apply { deleteRecursively(); mkdirs() }
        contentResolver.openInputStream(uri)?.use { input ->
            ZipInputStream(input.buffered()).use { zip ->
                while (true) {
                    val e = zip.nextEntry ?: break
                    if (!e.isDirectory && e.name.lowercase().endsWith(".webp")) {
                        val safe = File(e.name).name
                        dir.resolve(safe).outputStream().use { zip.copyTo(it) }
                    }
                    zip.closeEntry()
                }
            }
        }
        show(dir.listFiles()?.filter { it.extension.equals("webp", true) }?.sortedBy { it.name }.orEmpty())
    }

    private fun show(files: List<File>) {
        adapter.submit(files)
        status.text = "${files.size}枚"
        recycler.scrollToPosition(0)
    }
}

private class CardAdapter : RecyclerView.Adapter<CardVH>() {
    private var files: List<File> = emptyList()
    fun submit(value: List<File>) { files = value; notifyDataSetChanged() }
    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): CardVH {
        val v = LayoutInflater.from(parent.context).inflate(R.layout.item_card, parent, false)
        return CardVH(v)
    }
    override fun getItemCount() = files.size
    override fun onBindViewHolder(holder: CardVH, position: Int) = holder.bind(files[position])
}

private class CardVH(view: View) : RecyclerView.ViewHolder(view) {
    private val image: ImageView = view.findViewById(R.id.image)
    fun bind(file: File) {
        image.setImageBitmap(BitmapFactory.decodeFile(file.absolutePath))
    }
}
