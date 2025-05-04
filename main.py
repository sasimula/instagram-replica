from fastapi import FastAPI, HTTPException, Request, Depends, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from google.cloud import firestore
from google.cloud import storage
from google.oauth2 import service_account
from google.api_core import exceptions
from contextlib import asynccontextmanager
import datetime
import os
import uuid
import requests
import json

# Load Firebase service account credentials
service_account_path = 'instagram-replica-sasi-firebase-adminsdk-fbsvc-2e808997a0.json'  # Make sure this file is in your project root
credentials = service_account.Credentials.from_service_account_file(
    service_account_path
)

# Global bucket variable
bucket = None

def initialize_storage():
    """Initialize Firebase Storage connection"""
    global bucket
    try:
        # Initialize storage client
        storage_client = storage.Client(credentials=credentials)
        
        # Correct bucket name from your Firebase Console
        bucket_name = "instagram-replica-sasi.firebasestorage.app"
        
        # Get the bucket
        bucket = storage_client.get_bucket(bucket_name)
        print(f"Successfully connected to Firebase Storage: {bucket_name}")
        return True
    except exceptions.NotFound:
        print(f"ERROR: Firebase Storage bucket not found!")
        return False
    except Exception as e:
        print(f"Error connecting to Firebase Storage: {e}")
        return False

# Helper function to serialize datetime objects
def serialize_datetime(dt):
    """Convert datetime or DatetimeWithNanoseconds to ISO format string"""
    if hasattr(dt, 'isoformat'):
        return dt.isoformat()
    elif hasattr(dt, 'strftime'):
        return dt.strftime('%Y-%m-%dT%H:%M:%S.%f')
    else:
        return str(dt)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # This code runs when the application starts
    print("Starting Instagram Replica application...")
    print("Using Firebase authentication and storage")
    
    # Initialize storage
    if not initialize_storage():
        print("WARNING: Firebase Storage is not available")
        print("Image uploads will not work until Storage is properly configured")
    
    print("Remember to create composite index on Post collection:")
    print("- Username (ascending)")
    print("- Date (descending)")
    
    yield  # The application runs here
    
    # This code runs when the application shuts down
    print("Shutting down Instagram Replica application...")

# Initialize FastAPI app with lifespan handler
app = FastAPI(lifespan=lifespan)

# Configure templates directory
templates = Jinja2Templates(directory="templates")

# Initialize Firestore client with Firebase credentials
db = firestore.Client(credentials=credentials)

# Serve static files (CSS, JS, images)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Firebase configuration for token verification
FIREBASE_API_KEY = "AIzaSyDwK2f-L1H21HfqP9xJBxJpKhfVUjizEqw"
FIREBASE_PROJECT_ID = "instagram-replica-sasi"

# Helper function to verify Firebase ID token
async def verify_firebase_token(token: str):
    """Verify Firebase ID token using Firebase REST API"""
    try:
        # Use Firebase's REST API to verify the token
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={FIREBASE_API_KEY}"
        
        payload = {
            "idToken": token
        }
        
        response = requests.post(url, json=payload)
        
        if response.status_code == 200:
            data = response.json()
            if 'users' in data and len(data['users']) > 0:
                user = data['users'][0]
                return {
                    'uid': user['localId'],
                    'email': user.get('email', ''),
                    'displayName': user.get('displayName', '')
                }
        
        return None
    except Exception as e:
        print(f"Token verification error: {e}")
        return None

# Helper function to get current user
async def get_current_user(request: Request):
    """Get current user from verified token"""
    # Try to get token from cookie first
    token = request.cookies.get("token")
    
    # If not in cookie, check Authorization header as fallback
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
    
    if not token:
        raise HTTPException(status_code=401, detail="No valid auth token")
    
    # Verify token with Firebase
    user_info = await verify_firebase_token(token)
    
    if not user_info:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    user_id = user_info['uid']
    email = user_info.get('email', '')
    
    # Check if user exists in Firestore
    user_ref = db.collection('User').document(user_id)
    user_doc = user_ref.get()
    
    # If user doesn't exist, create them (first login)
    if not user_doc.exists:
        # Initialize user with required fields
        user_data = {
            'email': email,
            'username': email.split('@')[0],  # Default username from email
            'profileName': email.split('@')[0],  # For search functionality
            'followers': [],  # List of user IDs who follow this user
            'following': [],  # List of user IDs this user follows
            'createdAt': datetime.datetime.now(),
            'postsCount': 0,
            'followersCount': 0,
            'followingCount': 0
        }
        user_ref.set(user_data)
        return {'id': user_id, **user_data}
    
    user_data = user_doc.to_dict()
    return {'id': user_id, **user_data}

# Homepage route - redirects to login
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Homepage redirects to login"""
    return RedirectResponse(url="/login", status_code=303)

# Login page
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page with email/password form"""
    return templates.TemplateResponse(
        "login.html",
        {"request": request}
    )

# Register page
@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """Registration page with email/password form"""
    return templates.TemplateResponse(
        "register.html",
        {"request": request}
    )

# Main timeline page (protected)
@app.get("/timeline", response_class=HTMLResponse)
async def timeline(request: Request):
    """Main timeline showing posts from user and following users"""
    try:
        current_user = await get_current_user(request)
        
        # Get current user's ID and following list
        user_id = current_user['id']
        following = current_user.get('following', [])
        
        # Create list of user IDs to get posts from (self + following)
        user_ids = [user_id] + following
        
        # Query posts from these users
        posts_ref = db.collection('Post')
        
        # If no following users, just get user's own posts
        if len(user_ids) == 1:
            query = posts_ref.where('userId', '==', user_id).order_by('Date', direction=firestore.Query.DESCENDING).limit(50)
        else:
            # Firestore 'in' queries are limited to 10 items
            if len(user_ids) > 10:
                user_ids = user_ids[:10]
            query = posts_ref.where('userId', 'in', user_ids).order_by('Date', direction=firestore.Query.DESCENDING).limit(50)
        
        posts = []
        for doc in query.stream():
            post_data = doc.to_dict()
            post_data['id'] = doc.id
            
            # Get comments count for each post
            comments_count = len(post_data.get('comments', []))
            post_data['commentsCount'] = comments_count
            
            posts.append(post_data)
        
        return templates.TemplateResponse(
            "timeline.html",
            {
                "request": request,
                "current_user": current_user,
                "posts": posts
            }
        )
    except HTTPException as e:
        if e.status_code == 401:
            return RedirectResponse(url="/login", status_code=303)
        raise e

# Profile page
@app.get("/profile/{user_id}", response_class=HTMLResponse)
async def profile(request: Request, user_id: str):
    """User profile page"""
    try:
        current_user = await get_current_user(request)
        
        # Get profile user data
        user_ref = db.collection('User').document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            raise HTTPException(status_code=404, detail="User not found")
        
        profile_user = user_doc.to_dict()
        profile_user['id'] = user_id
        
        # Check if current user is following this profile
        is_following = user_id in current_user.get('following', [])
        is_own_profile = user_id == current_user['id']
        
        # Get user's posts in reverse chronological order
        posts_ref = db.collection('Post')
        posts_query = posts_ref.where('userId', '==', user_id).order_by('Date', direction=firestore.Query.DESCENDING)
        
        posts = []
        for doc in posts_query.stream():
            post_data = doc.to_dict()
            post_data['id'] = doc.id
            posts.append(post_data)
        
        return templates.TemplateResponse(
            "profile.html",
            {
                "request": request,
                "current_user": current_user,
                "profile_user": profile_user,
                "posts": posts,
                "is_following": is_following,
                "is_own_profile": is_own_profile
            }
        )
    except HTTPException as e:
        if e.status_code == 401:
            return RedirectResponse(url="/login", status_code=303)
        raise e

# Create post endpoint
@app.post("/create-post")
async def create_post(
    request: Request,
    caption: str = Form(...),
    image: UploadFile = File(...)
):
    """Create a new post with image and caption"""
    try:
        # Check if bucket is available
        if bucket is None:
            raise HTTPException(
                status_code=503, 
                detail="Firebase Storage is not available. Please check server logs."
            )
        
        current_user = await get_current_user(request)
        
        # Validate image type
        if image.content_type not in ["image/jpeg", "image/png"]:
            raise HTTPException(status_code=400, detail="Only JPG and PNG images are allowed")
        
        # Generate unique filename
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        file_extension = image.filename.split('.')[-1]
        file_name = f"{current_user['id']}_{timestamp}_{uuid.uuid4().hex[:8]}.{file_extension}"
        
        # Create blob path in Firebase Storage
        blob_path = f"posts/{current_user['id']}/{file_name}"
        blob = bucket.blob(blob_path)
        
        # Read the file content
        file_content = await image.read()
        
        # Upload to Firebase Storage
        blob.upload_from_string(
            file_content,
            content_type=image.content_type
        )
        
        # Make the file publicly accessible
        blob.make_public()
        
        # Get the public URL
        image_url = blob.public_url
        
        # Create post document in Firestore
        post_data = {
            'userId': current_user['id'],
            'Username': current_user['username'],  # Required field with exact capitalization
            'Date': datetime.datetime.now(),  # Required field with exact capitalization
            'caption': caption,
            'imageUrl': image_url,
            'likes': [],
            'comments': []
        }
        
        # Add post to Firestore
        post_ref = db.collection('Post').add(post_data)
        
        # Update user's post count
        user_ref = db.collection('User').document(current_user['id'])
        user_ref.update({
            'postsCount': firestore.Increment(1)
        })
        
        return RedirectResponse(url=f"/profile/{current_user['id']}", status_code=303)
    
    except Exception as e:
        print(f"Error creating post: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to create post: {str(e)}")

# Follow/Unfollow endpoint
@app.post("/follow/{user_id}")
async def follow_user(request: Request, user_id: str):
    """Follow or unfollow a user"""
    try:
        current_user = await get_current_user(request)
        
        if user_id == current_user['id']:
            raise HTTPException(status_code=400, detail="Cannot follow yourself")
        
        # Get both user documents
        current_user_ref = db.collection('User').document(current_user['id'])
        target_user_ref = db.collection('User').document(user_id)
        
        # Check if already following
        is_following = user_id in current_user.get('following', [])
        
        if is_following:
            # Unfollow
            current_user_ref.update({
                'following': firestore.ArrayRemove([user_id]),
                'followingCount': firestore.Increment(-1)
            })
            target_user_ref.update({
                'followers': firestore.ArrayRemove([current_user['id']]),
                'followersCount': firestore.Increment(-1)
            })
            action = "unfollowed"
        else:
            # Follow
            current_user_ref.update({
                'following': firestore.ArrayUnion([user_id]),
                'followingCount': firestore.Increment(1)
            })
            target_user_ref.update({
                'followers': firestore.ArrayUnion([current_user['id']]),
                'followersCount': firestore.Increment(1)
            })
            action = "followed"
        
        return JSONResponse({"status": "success", "action": action})
    except HTTPException as e:
        if e.status_code == 401:
            return JSONResponse({"status": "error", "message": "Not authenticated"}, status_code=401)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=e.status_code)

# Search users endpoint
@app.get("/search")
async def search_users(request: Request, q: str):
    """Search users by profile name (prefix matching)"""
    try:
        current_user = await get_current_user(request)
        
        if not q:
            return JSONResponse({"users": []})
        
        # Query users where profileName starts with search query
        users_ref = db.collection('User')
        
        # Firestore range query for prefix matching
        query = users_ref.where('profileName', '>=', q).where('profileName', '<=', q + '\uf8ff')
        
        users = []
        for doc in query.stream():
            user_data = doc.to_dict()
            users.append({
                'id': doc.id,
                'username': user_data.get('username'),
                'profileName': user_data.get('profileName'),
                'email': user_data.get('email')
            })
        
        return JSONResponse({"users": users})
    except HTTPException as e:
        if e.status_code == 401:
            return JSONResponse({"users": []}, status_code=401)
        raise e

# Followers list page
@app.get("/followers/{user_id}", response_class=HTMLResponse)
async def followers_list(request: Request, user_id: str):
    """Show list of followers for a user"""
    try:
        current_user = await get_current_user(request)
        
        # Get user data
        user_ref = db.collection('User').document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            raise HTTPException(status_code=404, detail="User not found")
        
        user_data = user_doc.to_dict()
        followers_ids = user_data.get('followers', [])
        
        # Get follower details
        followers = []
        for follower_id in followers_ids:
            follower_doc = db.collection('User').document(follower_id).get()
            if follower_doc.exists:
                follower_data = follower_doc.to_dict()
                followers.append({
                    'id': follower_id,
                    'username': follower_data.get('username'),
                    'profileName': follower_data.get('profileName')
                })
        
        # Sort by reverse chronological order (most recent first)
        followers.reverse()
        
        return templates.TemplateResponse(
            "followers.html",
            {
                "request": request,
                "current_user": current_user,
                "user": user_data,
                "user_id": user_id,
                "followers": followers
            }
        )
    except HTTPException as e:
        if e.status_code == 401:
            return RedirectResponse(url="/login", status_code=303)
        raise e

# Following list page
@app.get("/following/{user_id}", response_class=HTMLResponse)
async def following_list(request: Request, user_id: str):
    """Show list of users that a user is following"""
    try:
        current_user = await get_current_user(request)
        
        # Get user data
        user_ref = db.collection('User').document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            raise HTTPException(status_code=404, detail="User not found")
        
        user_data = user_doc.to_dict()
        following_ids = user_data.get('following', [])
        
        # Get following details
        following = []
        for following_id in following_ids:
            following_doc = db.collection('User').document(following_id).get()
            if following_doc.exists:
                following_data = following_doc.to_dict()
                following.append({
                    'id': following_id,
                    'username': following_data.get('username'),
                    'profileName': following_data.get('profileName')
                })
        
        # Sort by reverse chronological order (most recent first)
        following.reverse()
        
        return templates.TemplateResponse(
            "following.html",
            {
                "request": request,
                "current_user": current_user,
                "user": user_data,
                "user_id": user_id,
                "following": following
            }
        )
    except HTTPException as e:
        if e.status_code == 401:
            return RedirectResponse(url="/login", status_code=303)
        raise e

# Add comment to post
@app.post("/comment/{post_id}")
async def add_comment(
    request: Request,
    post_id: str,
    content: str = Form(...)
):
    """Add a comment to a post"""
    try:
        current_user = await get_current_user(request)
        
        # Validate comment length
        if len(content) > 200:
            raise HTTPException(status_code=400, detail="Comment must be 200 characters or less")
        
        # Get post reference
        post_ref = db.collection('Post').document(post_id)
        post_doc = post_ref.get()
        
        if not post_doc.exists:
            raise HTTPException(status_code=404, detail="Post not found")
        
        # Create comment data
        comment_data = {
            'userId': current_user['id'],
            'username': current_user['username'],
            'content': content,
            'createdAt': datetime.datetime.now()
        }
        
        # Add comment to post
        post_ref.update({
            'comments': firestore.ArrayUnion([comment_data])
        })
        
        return JSONResponse({"status": "success"})
    except HTTPException as e:
        if e.status_code == 401:
            return JSONResponse({"status": "error", "message": "Not authenticated"}, status_code=401)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=e.status_code)

# Get comments for a post (UPDATED TO FIX DATETIME SERIALIZATION)
@app.get("/comments/{post_id}")
async def get_comments(request: Request, post_id: str, show_all: bool = False):
    """Get comments for a post"""
    try:
        current_user = await get_current_user(request)
        
        # Get post document
        post_ref = db.collection('Post').document(post_id)
        post_doc = post_ref.get()
        
        if not post_doc.exists:
            raise HTTPException(status_code=404, detail="Post not found")
        
        post_data = post_doc.to_dict()
        comments = post_data.get('comments', [])
        
        # Sort comments by reverse chronological order
        comments.sort(key=lambda x: x.get('createdAt', datetime.datetime.min), reverse=True)
        
        # Convert datetime objects to ISO format strings
        serializable_comments = []
        for comment in comments:
            comment_copy = comment.copy()
            if 'createdAt' in comment_copy:
                comment_copy['createdAt'] = serialize_datetime(comment_copy['createdAt'])
            serializable_comments.append(comment_copy)
        
        # Apply pagination
        if not show_all and len(serializable_comments) > 5:
            display_comments = serializable_comments[:5]
            has_more = True
        else:
            display_comments = serializable_comments
            has_more = False
        
        return JSONResponse({
            "comments": display_comments,
            "has_more": has_more,
            "total_comments": len(comments)
        })
    except HTTPException as e:
        if e.status_code == 401:
            return JSONResponse({"comments": [], "has_more": False, "total_comments": 0}, status_code=401)
        raise e
    except Exception as e:
        print(f"Error in get_comments: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to retrieve comments")

# Run the application
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)