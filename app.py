
from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import logging
from pymongo import MongoClient, DESCENDING, UpdateOne
from gridfs import GridFS
from dotenv import load_dotenv
from datetime import datetime, timezone
import json
import io # Added import
from werkzeug.utils import secure_filename # Added import
from pdf2image import convert_from_bytes, pdfinfo_from_bytes # Added import
from pdf2image.exceptions import ( # Added import
    PDFInfoNotInstalledError,
    PDFPageCountError,
    PDFSyntaxError,
    PDFPopplerTimeoutError
)

# --- Initial Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
app_logger = logging.getLogger(__name__)
app_logger.info("Flask app.py: Script execution started.")

load_dotenv()
app_logger.info(f"Flask app.py: .env loaded: {'Yes' if os.getenv('MONGODB_URI') else 'No (or MONGODB_URI not set)'}")

from certificate_processor import extract_and_recommend_courses_from_image_data

app = Flask(__name__)
CORS(app)
app_logger.info("Flask app instance created.")

MONGODB_URI="mongodb+srv://gurupreetambodapati:MTXH7oEVPg3sJdg2@cluster0.fpsg1.mongodb.net/"
DB_NAME="imageverse_db"

if not MONGODB_URI:
    app.logger.critical("MONGODB_URI is not set. Please set it in your .env file or environment variables.")

mongo_client = None
db = None
fs_images = None 
user_course_processing_collection = None 
manual_course_names_collection = None

try:
    if MONGODB_URI:
        app.logger.info(f"Attempting to connect to MongoDB with URI (first part): {MONGODB_URI.split('@')[0] if '@' in MONGODB_URI else 'URI_FORMAT_UNEXPECTED'}")
        mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000) 
        mongo_client.admin.command('ismaster') 
        db = mongo_client[DB_NAME]
        fs_images = GridFS(db, collection="images") 
        user_course_processing_collection = db["user_course_processing_results"]
        manual_course_names_collection = db["manual_course_names"]
        # Create unique index for manual_course_names if it doesn't exist
        manual_course_names_collection.create_index([("userId", 1), ("fileId", 1)], unique=True, background=True)
        app.logger.info(f"Successfully connected to MongoDB: {DB_NAME}, GridFS bucket 'images', collection 'user_course_processing_results', and collection 'manual_course_names'.")
    else:
        app.logger.warning("MONGODB_URI not found, MongoDB connection will not be established.")
except Exception as e:
    app.logger.error(f"Failed to connect to MongoDB or initialize collections: {e}")
    mongo_client = None 
    db = None
    fs_images = None
    user_course_processing_collection = None
    manual_course_names_collection = None

POPPLER_PATH = os.getenv("POPPLER_PATH", None)
if POPPLER_PATH: app_logger.info(f"Flask app.py: POPPLER_PATH found: {POPPLER_PATH}")
else: app_logger.info("Flask app.py: POPPLER_PATH not set (pdf2image will try to find Poppler in PATH).")


@app.route('/', methods=['GET'])
def health_check():
    app_logger.info("Flask /: Health check endpoint hit.")
    return jsonify({"status": "Flask server is running", "message": "Welcome to ImageVerse Flask API!"}), 200

@app.route('/api/manual-course-name', methods=['POST'])
def save_manual_course_name():
    req_id_manual_name = datetime.now().strftime('%Y%m%d%H%M%S%f')
    app_logger.info(f"Flask /api/manual-course-name (Req ID: {req_id_manual_name}): Received request.")

    db_components_to_check = {
        "mongo_client": mongo_client,
        "db_instance": db,
        "manual_course_names_collection": manual_course_names_collection
    }
    missing_components = [name for name, comp in db_components_to_check.items() if comp is None]
    if missing_components:
        error_message = f"Database component(s) not available for manual name saving: {', '.join(missing_components)}."
        app_logger.error(f"Flask (Req ID: {req_id_manual_name}): {error_message}")
        return jsonify({"error": error_message, "errorKey": "DB_COMPONENT_UNAVAILABLE"}), 503

    data = request.get_json()
    user_id = data.get("userId")
    file_id = data.get("fileId")
    course_name = data.get("courseName")

    if not all([user_id, file_id, course_name]):
        app_logger.warning(f"Flask (Req ID: {req_id_manual_name}): Missing userId, fileId, or courseName.")
        return jsonify({"error": "Missing userId, fileId, or courseName"}), 400

    app_logger.info(f"Flask (Req ID: {req_id_manual_name}): Saving manual name for userId: {user_id}, fileId: {file_id}, courseName: '{course_name}'")

    try:
        update_result = manual_course_names_collection.update_one(
            {"userId": user_id, "fileId": file_id},
            {
                "$set": {
                    "courseName": course_name,
                    "updatedAt": datetime.now(timezone.utc)
                },
                "$setOnInsert": {"createdAt": datetime.now(timezone.utc)}
            },
            upsert=True
        )
        if update_result.upserted_id:
            app_logger.info(f"Flask (Req ID: {req_id_manual_name}): Inserted new manual course name. ID: {update_result.upserted_id}")
        elif update_result.modified_count > 0:
            app_logger.info(f"Flask (Req ID: {req_id_manual_name}): Updated existing manual course name.")
        else:
             app_logger.info(f"Flask (Req ID: {req_id_manual_name}): Manual course name was already up-to-date (no change). Matched: {update_result.matched_count}")
        
        return jsonify({"success": True, "message": "Manual course name saved."}), 200
    except Exception as e:
        app_logger.error(f"Flask (Req ID: {req_id_manual_name}): Error saving manual course name for userId {user_id}, fileId {file_id}: {str(e)}", exc_info=True)
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500


@app.route('/api/process-certificates', methods=['POST'])
def process_certificates_from_db():
    req_id_cert = datetime.now().strftime('%Y%m%d%H%M%S%f')
    app_logger.info(f"Flask /api/process-certificates (Req ID: {req_id_cert}): Received request.")
    
    db_components_to_check = {
        "mongo_client": mongo_client,
        "db_instance": db,
        "gridfs_images_bucket": fs_images,
        "user_course_processing_collection": user_course_processing_collection,
        "manual_course_names_collection": manual_course_names_collection
    }
    missing_components = [name for name, comp in db_components_to_check.items() if comp is None]

    if missing_components:
        error_message = f"Database component(s) not available for certificate processing: {', '.join(missing_components)}. Check MongoDB connection and initialization."
        app_logger.error(f"Flask (Req ID: {req_id_cert}): {error_message}")
        return jsonify({"error": error_message, "errorKey": "DB_COMPONENT_UNAVAILABLE"}), 503

    data = request.get_json()
    user_id = data.get("userId")
    processing_mode = data.get("mode", "ocr_only") 
    additional_manual_courses_general = data.get("additionalManualCourses", []) 
    known_course_names_from_frontend = data.get("knownCourseNames", [])

    if not user_id:
        app_logger.warning(f"Flask (Req ID: {req_id_cert}): User ID not provided.")
        return jsonify({"error": "User ID (userId) not provided"}), 400

    app_logger.info(f"Flask (Req ID: {req_id_cert}): Processing for userId: {user_id}, Mode: {processing_mode}.")
    app_logger.info(f"Flask (Req ID: {req_id_cert}): General Manual Courses: {additional_manual_courses_general}")
    app_logger.info(f"Flask (Req ID: {req_id_cert}): Known Course Names for Suggestions: {known_course_names_from_frontend}")


    try:
        processing_result_dict = {}
        latest_previous_user_data_list = [] 
        user_all_image_ids_associated_with_run = []

        if processing_mode == 'ocr_only':
            image_data_for_ocr_processing = []
            user_image_files_cursor = db.images.files.find({"metadata.userId": user_id}) # Use db instance directly
            for file_doc in user_image_files_cursor:
                file_id = file_doc["_id"]
                original_filename = file_doc.get("metadata", {}).get("originalName", file_doc["filename"])
                content_type = file_doc.get("contentType", "application/octet-stream") 
                user_all_image_ids_associated_with_run.append(str(file_id))
                
                app_logger.info(f"Flask (Req ID: {req_id_cert}, OCR_MODE): Fetching file: ID={file_id}, Name={original_filename}")
                grid_out = fs_images.get(file_id) # Use fs_images instance directly
                image_bytes = grid_out.read()
                grid_out.close()
                
                effective_content_type = file_doc.get("metadata", {}).get("sourceContentType", content_type)
                if file_doc.get("metadata", {}).get("convertedTo"): 
                     effective_content_type = file_doc.get("metadata", {}).get("convertedTo")

                image_data_for_ocr_processing.append({
                    "bytes": image_bytes, "original_filename": original_filename, 
                    "content_type": effective_content_type, "file_id": str(file_id) 
                })
            app_logger.info(f"Flask (Req ID: {req_id_cert}, OCR_MODE): Found {len(image_data_for_ocr_processing)} images for OCR attempt.")
            
            if not image_data_for_ocr_processing and not additional_manual_courses_general:
                 app_logger.info(f"Flask (Req ID: {req_id_cert}, OCR_MODE): No images and no general manual courses. Returning empty handed.")
                 return jsonify({
                    "successfully_extracted_courses": [],
                    "failed_extraction_images": [],
                    "processed_image_file_ids": []
                 }), 200
            
            ocr_phase_raw_results = extract_and_recommend_courses_from_image_data(
                image_data_list=image_data_for_ocr_processing,
                mode='ocr_only',
                additional_manual_courses=additional_manual_courses_general 
            )
            app_logger.info(f"Flask (Req ID: {req_id_cert}, OCR_MODE): Initial OCR processing by certificate_processor complete.")

            # --- Apply stored manual names ---
            current_successful_courses = ocr_phase_raw_results.get("successfully_extracted_courses", [])
            initial_failed_images = ocr_phase_raw_results.get("failed_extraction_images", [])
            final_failed_images_for_frontend = []

            if initial_failed_images:
                app_logger.info(f"Flask (Req ID: {req_id_cert}, OCR_MODE): {len(initial_failed_images)} images failed initial OCR. Checking for stored manual names.")
                stored_manual_names_cursor = manual_course_names_collection.find({"userId": user_id})
                stored_manual_names_map = {item["fileId"]: item["courseName"] for item in stored_manual_names_cursor}
                app_logger.info(f"Flask (Req ID: {req_id_cert}, OCR_MODE): Found {len(stored_manual_names_map)} stored manual names for user {user_id}.")

                for failed_img_info in initial_failed_images:
                    file_id_of_failed_img = failed_img_info.get("file_id")
                    if file_id_of_failed_img in stored_manual_names_map:
                        stored_name = stored_manual_names_map[file_id_of_failed_img]
                        app_logger.info(f"Flask (Req ID: {req_id_cert}, OCR_MODE): Found stored manual name '{stored_name}' for failed image fileId {file_id_of_failed_img}. Adding to successful courses.")
                        if stored_name not in current_successful_courses:
                            current_successful_courses.append(stored_name)
                        # This image is no longer considered "failed" for frontend prompting
                    else:
                        # No stored name, so it's a true failure for frontend prompting
                        final_failed_images_for_frontend.append(failed_img_info)
                
                ocr_phase_raw_results["successfully_extracted_courses"] = sorted(list(set(current_successful_courses)))
                ocr_phase_raw_results["failed_extraction_images"] = final_failed_images_for_frontend
                app_logger.info(f"Flask (Req ID: {req_id_cert}, OCR_MODE): After applying stored names - Successful: {len(current_successful_courses)}, Failures to prompt: {len(final_failed_images_for_frontend)}.")
            else:
                 app_logger.info(f"Flask (Req ID: {req_id_cert}, OCR_MODE): No images initially failed OCR. No need to check stored manual names.")
                 ocr_phase_raw_results["successfully_extracted_courses"] = sorted(list(set(current_successful_courses)))


            processing_result_dict = ocr_phase_raw_results
            processing_result_dict["processed_image_file_ids"] = list(set(user_all_image_ids_associated_with_run)) # Ensure this is passed back

        elif processing_mode == 'suggestions_only':
            if not known_course_names_from_frontend: 
                return jsonify({"user_processed_data": [], "llm_error_summary": "No course names provided for suggestion generation."}), 200

            try:
                latest_doc = user_course_processing_collection.find_one(
                    {"userId": user_id},
                    sort=[("processedAt", DESCENDING)],
                    projection={"user_processed_data": 1} 
                )
                if latest_doc and "user_processed_data" in latest_doc:
                    latest_previous_user_data_list = latest_doc["user_processed_data"]
                    app_logger.info(f"Flask (Req ID: {req_id_cert}, SUGGEST_MODE): Fetched 'user_processed_data' from latest record for cache.")
                else:
                    app_logger.info(f"Flask (Req ID: {req_id_cert}, SUGGEST_MODE): No previous processed data found for cache.")
            except Exception as e:
                app_logger.error(f"Flask (Req ID: {req_id_cert}, SUGGEST_MODE): Error fetching latest processed data: {e}")

            processing_result_dict = extract_and_recommend_courses_from_image_data(
                mode='suggestions_only',
                known_course_names=known_course_names_from_frontend,
                previous_user_data_list=latest_previous_user_data_list
            )
            app_logger.info(f"Flask (Req ID: {req_id_cert}, SUGGEST_MODE): Suggestion processing complete.")

            current_processed_data_for_db = processing_result_dict.get("user_processed_data", [])
            should_store_new_result = True 

            if latest_previous_user_data_list:
                prev_course_names = set(item['identified_course_name'] for item in latest_previous_user_data_list)
                curr_course_names = set(item['identified_course_name'] for item in current_processed_data_for_db)
                
                if prev_course_names == curr_course_names:
                    prev_sug_counts = sum(len(item.get('llm_suggestions', [])) for item in latest_previous_user_data_list)
                    curr_sug_counts = sum(len(item.get('llm_suggestions', [])) for item in current_processed_data_for_db)
                    if abs(prev_sug_counts - curr_sug_counts) <= len(curr_course_names): 
                        should_store_new_result = False
                        app_logger.info(f"Flask (Req ID: {req_id_cert}, SUGGEST_MODE): New processing result seems similar to latest. Skipping storage.")
            
            if should_store_new_result and current_processed_data_for_db:
                try:
                    # Get all user image IDs to associate with this suggestion run record
                    # This is needed because suggestions are based on a consolidated list, not necessarily tied to images processed *in this exact OCR run*
                    # if some courses were already known or added manually.
                    user_all_image_ids_associated_with_suggestions = [str(doc["_id"]) for doc in db.images.files.find({"metadata.userId": user_id}, projection={"_id": 1})]

                    data_to_store_in_db = {
                        "userId": user_id,
                        "processedAt": datetime.now(timezone.utc),
                        "user_processed_data": current_processed_data_for_db,
                        "associated_image_file_ids": user_all_image_ids_associated_with_suggestions, 
                        "llm_error_summary_at_processing": processing_result_dict.get("llm_error_summary")
                    }
                    insert_result = user_course_processing_collection.insert_one(data_to_store_in_db)
                    app_logger.info(f"Flask (Req ID: {req_id_cert}, SUGGEST_MODE): Stored new structured processing result. Inserted ID: {insert_result.inserted_id}")
                    processing_result_dict["associated_image_file_ids"] = user_all_image_ids_associated_with_suggestions
                except Exception as e:
                    app_logger.error(f"Flask (Req ID: {req_id_cert}, SUGGEST_MODE): Error storing new structured result: {e}")
            elif not current_processed_data_for_db:
                 app_logger.info(f"Flask (Req ID: {req_id_cert}, SUGGEST_MODE): No user processed data generated, nothing to store or associate image IDs with for DB.")
                 processing_result_dict["associated_image_file_ids"] = []
            else: # should_store_new_result is False, but current_processed_data_for_db exists
                 app_logger.info(f"Flask (Req ID: {req_id_cert}, SUGGEST_MODE): Result not stored (similar to previous). Using image IDs from previous if available or querying all user images.")
                 # Try to get associated IDs from latest_doc if it's the source of similarity
                 latest_image_ids = latest_doc.get("associated_image_file_ids") if latest_doc else None
                 if latest_image_ids:
                     processing_result_dict["associated_image_file_ids"] = latest_image_ids
                 else: # Fallback to all user images
                     processing_result_dict["associated_image_file_ids"] = [str(doc["_id"]) for doc in db.images.files.find({"metadata.userId": user_id}, projection={"_id": 1})]


        else:
            app_logger.error(f"Flask (Req ID: {req_id_cert}): Invalid processing_mode '{processing_mode}'.")
            return jsonify({"error": f"Invalid processing mode: {processing_mode}"}), 400
        
        return jsonify(processing_result_dict)

    except Exception as e:
        app_logger.error(f"Flask (Req ID: {req_id_cert}): Error during certificate processing for user {user_id}: {str(e)}", exc_info=True)
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500

@app.route('/api/convert-pdf-to-images', methods=['POST'])
def convert_pdf_to_images_route():
    # Generate a unique ID for this request for logging correlation
    req_id = datetime.now().strftime('%Y%m%d%H%M%S%f')
    app.logger.info(f"Flask /api/convert-pdf-to-images (Req ID: {req_id}): Received request.")

    if mongo_client is None or db is None or fs_images is None:
        app.logger.error(f"Flask (Req ID: {req_id}): MongoDB connection or GridFS not available.")
        return jsonify({"error": "Database connection or GridFS not available. Check server logs."}), 503

    if 'pdf_file' not in request.files:
        app.logger.warning(f"Flask (Req ID: {req_id}): No 'pdf_file' part in the request.")
        return jsonify({"error": "No PDF file part in the request."}), 400

    pdf_file_storage = request.files['pdf_file']
    user_id = request.form.get('userId')
    original_pdf_name = request.form.get('originalName', pdf_file_storage.filename) 

    if not user_id:
        app.logger.warning(f"Flask (Req ID: {req_id}): Missing 'userId' in form data.")
        return jsonify({"error": "Missing 'userId' in form data."}), 400
    
    if not original_pdf_name:
        app.logger.warning(f"Flask (Req ID: {req_id}): No filename or originalName provided for PDF.")
        return jsonify({"error": "No filename or originalName provided for PDF."}), 400

    app.logger.info(f"Flask (Req ID: {req_id}): Processing PDF '{original_pdf_name}' for userId '{user_id}'.")

    try:
        pdf_bytes = pdf_file_storage.read()
        
        # Poppler self-check using pdfinfo_from_bytes
        try:
            app.logger.info(f"Flask (Req ID: {req_id}): Using POPPLER_PATH for pdfinfo: '{POPPLER_PATH if POPPLER_PATH else 'System Default'}'")
            pdfinfo = pdfinfo_from_bytes(pdf_bytes, userpw=None, poppler_path=POPPLER_PATH)
            app.logger.info(f"Flask (Req ID: {req_id}): Poppler self-check (pdfinfo) successful. PDF Info: {pdfinfo}")
        except PDFInfoNotInstalledError:
            app.logger.error(f"Flask (Req ID: {req_id}): CRITICAL - Poppler (pdfinfo) utilities not found or not executable (POPPLER_PATH: '{POPPLER_PATH}'). Please ensure 'poppler-utils' is installed and in the system PATH for the Flask server environment or POPPLER_PATH is correctly set in .env.")
            return jsonify({"error": "PDF processing utilities (Poppler/pdfinfo) are not installed or configured correctly on the server."}), 500
        except PDFPopplerTimeoutError:
            app.logger.error(f"Flask (Req ID: {req_id}): Poppler (pdfinfo) timed out processing the PDF. The PDF might be too complex or corrupted.")
            return jsonify({"error": "Timeout during PDF information retrieval. The PDF may be too complex or corrupted."}), 400
        except Exception as info_err: # Catch other potential errors from pdfinfo
            app.logger.error(f"Flask (Req ID: {req_id}): Error getting PDF info with Poppler: {str(info_err)}", exc_info=True)
            return jsonify({"error": f"Failed to retrieve PDF info: {str(info_err)}"}), 500

        app.logger.info(f"Flask (Req ID: {req_id}): Attempting to convert PDF bytes to images using pdf2image (convert_from_bytes). POPPLER_PATH for conversion: '{POPPLER_PATH if POPPLER_PATH else 'System Default'}'")
        images_from_pdf = convert_from_bytes(pdf_bytes, dpi=200, fmt='png', poppler_path=POPPLER_PATH) 
        app.logger.info(f"Flask (Req ID: {req_id}): PDF '{original_pdf_name}' converted to {len(images_from_pdf)} image(s).")

        converted_files_metadata = []

        for i, image_pil in enumerate(images_from_pdf):
            page_number = i + 1
            base_pdf_name_secure = secure_filename(os.path.splitext(original_pdf_name)[0])
            gridfs_filename = f"{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{base_pdf_name_secure}_page_{page_number}.png"
            
            img_byte_arr = io.BytesIO()
            image_pil.save(img_byte_arr, format='PNG')
            img_byte_arr_val = img_byte_arr.getvalue()

            metadata_for_gridfs = {
                "originalName": f"{original_pdf_name} (Page {page_number})", 
                "userId": user_id,
                "uploadedAt": datetime.utcnow().isoformat(),
                "sourceContentType": "application/pdf", # Original was PDF
                "convertedTo": "image/png", # Stored as PNG
                "pageNumber": page_number,
                "reqIdParent": req_id 
            }
            
            app.logger.info(f"Flask (Req ID: {req_id}): Storing page {page_number} as '{gridfs_filename}' in GridFS with metadata: {metadata_for_gridfs}")
            # The actual contentType for GridFS should be 'image/png' for the converted file
            file_id_obj = fs_images.put(img_byte_arr_val, filename=gridfs_filename, contentType='image/png', metadata=metadata_for_gridfs)
            
            converted_files_metadata.append({
                "originalName": metadata_for_gridfs["originalName"],
                "fileId": str(file_id_obj),
                "filename": gridfs_filename,
                "contentType": 'image/png', # This is the type of the file stored in GridFS
                "pageNumber": page_number
            })
            app.logger.info(f"Flask (Req ID: {req_id}): Stored page {page_number} with GridFS ID: {str(file_id_obj)}.")

        app.logger.info(f"Flask (Req ID: {req_id}): Successfully processed and stored {len(converted_files_metadata)} pages for PDF '{original_pdf_name}'.")
        return jsonify({"message": "PDF converted and pages stored successfully.", "converted_files": converted_files_metadata}), 200

    except PDFPageCountError:
        app.logger.error(f"Flask (Req ID: {req_id}): pdf2image could not get page count for '{original_pdf_name}'. PDF might be corrupted or password-protected.", exc_info=True)
        return jsonify({"error": "Could not determine page count. The PDF may be corrupted or password-protected."}), 400
    except PDFSyntaxError:
        app.logger.error(f"Flask (Req ID: {req_id}): pdf2image encountered syntax error for '{original_pdf_name}'. PDF is likely corrupted.", exc_info=True)
        return jsonify({"error": "PDF syntax error. The file may be corrupted."}), 400
    except PDFPopplerTimeoutError: # This catches timeouts during the convert_from_bytes call
        app.logger.error(f"Flask (Req ID: {req_id}): Poppler (conversion) timed out processing PDF '{original_pdf_name}'.")
        return jsonify({"error": "Timeout during PDF page conversion. The PDF may be too complex."}), 400
    except Exception as e:
        app.logger.error(f"Flask (Req ID: {req_id}): Error during PDF conversion or storage for '{original_pdf_name}': {str(e)}", exc_info=True)
        # Check if the error string or type suggests Poppler is not installed (during conversion stage)
        if "PopplerNotInstalledError" in str(type(e)) or "pdftoppm" in str(e).lower() or "pdfinfo" in str(e).lower():
             app.logger.error(f"Flask (Req ID: {req_id}): CRITICAL - Poppler utilities (pdftoppm/pdfinfo) not found or not executable (POPPLER_PATH: '{POPPLER_PATH}').")
             return jsonify({"error": "PDF processing utilities (Poppler) are not installed or configured correctly on the server (conversion stage)."}), 500
        return jsonify({"error": f"An unexpected error occurred during PDF processing: {str(e)}"}), 500


if __name__ == '__main__':
    app.logger.info("Flask application starting with __name__ == '__main__'")
    app_logger.info(f"Effective MONGODB_URI configured: {'Yes' if MONGODB_URI else 'No'}")
    app_logger.info(f"Effective MONGODB_DB_NAME: {DB_NAME}")
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=True)

    

  

    